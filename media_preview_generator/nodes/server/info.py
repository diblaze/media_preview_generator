"""What a worker node has: GPUs, CPU, RAM, temp space, FFmpeg (the ``worker`` block of ``GET /v1/info``).

Imported only when the worker role is on: GPU detection pulls in the app's FFmpeg probing, which the slim Plex
database builds don't carry. GPU detection runs FFmpeg once per device (about a second), so its answer is cached and
re-checked in the background every :data:`GPU_RECHECK_S`, at once after a task reports a GPU failure, and on
``POST /v1/gpus/rescan``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from .tasks import probe_temp_folder

GPU_RECHECK_S = 300.0
FFMPEG_VERSION_TIMEOUT_S = 30.0


def _gpu_entry(raw: object) -> dict[str, Any]:
    """One ``(type, device, details)`` tuple from ``detect_all_gpus`` as the protocol's JSON entry."""
    gpu_type, device, details = raw if isinstance(raw, tuple | list) and len(raw) == 3 else (raw, "", {})
    details = details if isinstance(details, dict) else {}
    error = details.get("error")
    device = str(device or "")
    # An NVIDIA card that passes its test is cuda:<index>; one that fails it (and Windows' single card) comes back as
    # bare "cuda". Both mean the first card, and a worker group member saved as cuda:0 must still find it.
    if device == "cuda":
        device = "cuda:0"
    return {
        "type": str(gpu_type or ""),
        "device": device,
        "name": str(details.get("name") or gpu_type or ""),
        # detect_all_gpus only adds a status to entries that can fail; one without it was tested working.
        "status": str(details.get("status") or "ok"),
        "error": str(error) if error else None,
    }


def _credit_text(gpu_type: str, device: str) -> dict[str, Any] | None:
    """Where this GPU's credits text detection runs, once a credits task has found out; None before that.

    Only read from the text-detection pool when a credits op has already started it: starting one here would spawn
    ONNX helper processes and run their self-test on every /v1/info.
    """
    module = sys.modules.get("media_preview_generator.markers.credits.textdet_helper")
    if module is None:
        return None
    with module._pool_lock:
        pool = module._pool
    if pool is None:
        return None
    backend = pool.backend_of(gpu_type, device)
    if backend is None:
        return None
    if backend == "webgpu":
        return {"on_gpu": True, "reason": ""}
    key = module.device_key(gpu_type, device)
    reasons = [row["reason"] for row in pool.credits_on_cpu() if row["key"] == key and row.get("reason")]
    return {"on_gpu": False, "reason": reasons[0] if reasons else "its GPU can't run text detection"}


def _meminfo(path: str = "/proc/meminfo") -> tuple[int | None, int | None]:
    values: dict[str, int] = {}
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[name] = int(parts[0]) * 1024
    except OSError:
        return None, None
    return values.get("MemTotal"), values.get("MemAvailable")


def _load1() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):  # Windows has no load average
        return None


def _ffmpeg() -> dict[str, Any]:
    from ...config import _resolve_ffmpeg_path

    path = _resolve_ffmpeg_path()
    if not path:
        return {"path": None, "version": None}
    try:
        done = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=FFMPEG_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("FFmpeg at {} didn't answer -version: {}", path, exc)
        return {"path": path, "version": None}
    first = (done.stdout or "").splitlines()[0].strip() if done.stdout else ""
    return {"path": path, "version": first or None}


class WorkerInfo:
    """The worker block, with GPU detection cached and re-checked in the background.

    Args:
        temp_root: The node's task temp root (its free space is reported).
        recheck_s: Background GPU re-check interval.
        detect: For tests; default ``gpu.detect.detect_all_gpus``.
    """

    def __init__(
        self,
        *,
        temp_root: str,
        recheck_s: float = GPU_RECHECK_S,
        detect: Callable[[], list] | None = None,
    ) -> None:
        self._temp_root = temp_root
        self._recheck_s = recheck_s
        self._detect = detect
        self._gpus: list[dict[str, Any]] | None = None
        self._checked_at: float | None = None
        self._gpu_error: str | None = None
        self._ffmpeg: dict[str, Any] | None = None
        # Two locks: one detection at a time (each runs FFmpeg on every device), and a short one for the cached
        # answer, so /v1/info never waits out a re-check that may take a minute on a busy or wedged GPU.
        self._detect_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background re-check (its first pass detects at once, so the first /v1/info is quick)."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="node-gpu-recheck", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.rescan()
            self._wake.wait(self._recheck_s)
            self._wake.clear()

    def note_gpu_error(self) -> None:
        """A task's GPU failed: re-check the GPUs now rather than at the next interval."""
        self._wake.set()

    def rescan(self) -> list[dict[str, Any]]:
        """Run GPU detection now.

        A detection that raises keeps the last list it found, its error kept: one failed pass on a busy GPU must not
        read as a lost GPU. With no earlier list the node reports no GPUs and the error, which the app reads as a
        check still to come, not as a missing GPU.

        Returns:
            The JSON-safe GPU list now reported.
        """
        with self._detect_lock:
            detect = self._detect
            error: str | None = None
            try:
                if detect is None:
                    from ...gpu import detect as gpu_detect

                    detect = gpu_detect.detect_all_gpus
                found = [_gpu_entry(raw) for raw in detect() or []]
            except Exception as exc:  # noqa: BLE001 - a broken probe must not take /v1/info down with it
                logger.opt(exception=True).warning("GPU detection failed on this node; it keeps its last GPU list")
                with self._cache_lock:
                    found = list(self._gpus) if self._gpus is not None else []
                error = f"{type(exc).__name__}: {exc}"
            with self._cache_lock:
                self._gpus, self._checked_at, self._gpu_error = found, time.time(), error
            return [dict(entry) for entry in found]

    def gpus(self) -> list[dict[str, Any]]:
        """The cached GPU list (detected now if nothing has detected yet), with ``credit_text`` where known."""
        with self._cache_lock:
            cached = self._gpus
        found = cached if cached is not None else self.rescan()
        out = []
        for entry in found:
            row = dict(entry)
            try:
                credit = _credit_text(row["type"], row["device"])
            except Exception:  # noqa: BLE001 - an optional detail; /v1/info must still answer
                credit = None
            if credit is not None:
                row["credit_text"] = credit
            out.append(row)
        return out

    @property
    def gpu_checked_at(self) -> float | None:
        return self._checked_at

    @property
    def gpu_error(self) -> str | None:
        """Why the last GPU detection failed, or None when it worked."""
        return self._gpu_error

    def block(self, *, busy: int) -> dict[str, Any]:
        """The ``worker`` block of /v1/info.

        Args:
            busy: How many tasks are running.

        Returns:
            The block.
        """
        gpus = self.gpus()
        ram_total, ram_available = _meminfo()
        # Every start makes a folder there; one that can't would refuse each file the app sends, so the app reads
        # this and sends none.
        temp_problem = probe_temp_folder(self._temp_root)
        try:
            free_bytes: int | None = shutil.disk_usage(self._temp_root).free
        except OSError:
            free_bytes = None
        if self._ffmpeg is None:
            self._ffmpeg = _ffmpeg()
        return {
            "gpus": gpus,
            "cpu": {
                "cores": os.cpu_count() or 1,
                "load1": _load1(),
                "ram_total": ram_total,
                "ram_available": ram_available,
            },  # fmt: skip
            "temp": {"path": self._temp_root, "free_bytes": free_bytes, "problem": temp_problem},
            "busy": busy,
            "ffmpeg": dict(self._ffmpeg),
            "gpu_checked_at": self._checked_at,
            "gpu_error": self._gpu_error,
        }
