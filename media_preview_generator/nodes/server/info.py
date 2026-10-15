"""What a worker node has: GPUs, CPU, RAM, temp space, FFmpeg (the ``worker`` block of ``GET /v1/info``).

Imported only when the worker role is on: GPU detection pulls in the app's FFmpeg probing, which the slim Plex
database builds don't carry. GPU detection runs FFmpeg once per device (about a second), so its answer is cached and
re-checked in the background every :data:`GPU_RECHECK_S`, at once after a task reports a GPU failure, and on
``POST /v1/gpus/rescan``.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from . import nodelog
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


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as handle:
            text = handle.read().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _inactive_file(path: str, key: str) -> int:
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            for line in handle:
                name, _, value = line.partition(" ")
                if name == key and value.strip().isdigit():
                    return int(value)
    except OSError:
        pass
    return 0


def _cgroup_memory(cgroup_root: str = "/sys/fs/cgroup") -> tuple[int | None, int | None]:
    """A container's memory limit (``--memory``) and how much of it is free, from cgroup v2 or v1.

    Free is the limit less what the container uses, its page cache that the kernel drops first not counted (as
    ``docker stats`` counts it).

    Args:
        cgroup_root: Where the cgroup files are (v2's ``memory.max``, or v1's ``memory/memory.limit_in_bytes``).

    Returns:
        ``(limit, free)`` in bytes, or ``(None, None)`` with no limit to read (v2's ``max``, or not in a cgroup). v1's
        "no limit" is a huge number, which the caller's host figures cap.
    """
    v1 = os.path.join(cgroup_root, "memory")
    for folder, limit_name, usage_name, inactive_key in (
        (cgroup_root, "memory.max", "memory.current", "inactive_file"),
        (v1, "memory.limit_in_bytes", "memory.usage_in_bytes", "total_inactive_file"),
    ):
        if not os.path.exists(os.path.join(folder, limit_name)):
            continue
        limit = _read_int(os.path.join(folder, limit_name))
        usage = _read_int(os.path.join(folder, usage_name))
        if limit is None or usage is None:
            return None, None
        used = max(0, usage - _inactive_file(os.path.join(folder, "memory.stat"), inactive_key))
        return limit, max(0, limit - used)
    return None, None


def _memory(meminfo_path: str = "/proc/meminfo", cgroup_root: str = "/sys/fs/cgroup") -> tuple[int | None, int | None]:
    """The RAM this node may use and how much of it is free: the host's, capped by a container's memory limit.

    ``/proc/meminfo`` is the host's: a node started with ``--memory 2g`` on a 192 GB host said it had 192 GB.

    Returns:
        ``(total, available)`` in bytes; either is None when it can't be read.
    """
    total, available = _meminfo(meminfo_path)
    limit, free = _cgroup_memory(cgroup_root)
    if limit is not None:
        total = min(total, limit) if total is not None else limit
    if free is not None:
        available = min(available, free) if available is not None else free
    return total, available


def _cores(cpu_max_path: str = "/sys/fs/cgroup/cpu.max") -> int:
    """The CPU cores this node may use: the ones it may run on, capped by a container's CPU limit (``--cpus``).

    ``os.cpu_count()`` is the host's: a node started with ``--cpus 0.5`` on a 20-core host said it had 20.

    Args:
        cpu_max_path: The cgroup v2 ``cpu.max`` file (``"<quota> <period>"``, or ``"max <period>"`` for no limit).

    Returns:
        At least 1.
    """
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # macOS and Windows have no affinity call
        cores = os.cpu_count() or 1
    try:
        with open(cpu_max_path, encoding="ascii") as handle:
            quota, _, period = handle.read().strip().partition(" ")
        if quota != "max":
            cores = min(cores, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass  # no cgroup v2 limit to read: the affinity count stands
    return max(1, cores)


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
        # Set once any detection has stored its answer.
        self._detected = threading.Event()
        self._thread: threading.Thread | None = None
        self._temp_lock = threading.Lock()
        self._temp_logged: str | None = None

    def start(self) -> None:
        """Start the background re-check (its first pass detects at once, so the first /v1/info is quick).

        The temp folder is checked now too, so a node that can't use it says so in its own log from the start.
        """
        self._note_temp_problem(probe_temp_folder(self._temp_root))
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="node-gpu-recheck", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        first = True
        while not self._stop.is_set():
            self.rescan(quiet=not first)
            first = False
            self._wake.wait(self._recheck_s)
            self._wake.clear()

    def note_gpu_error(self) -> None:
        """A task's GPU failed: re-check the GPUs now rather than at the next interval."""
        self._wake.set()

    def rescan(self, *, quiet: bool = False) -> list[dict[str, Any]]:
        """Run GPU detection now.

        A detection that raises keeps the last list it found, its error kept: one failed pass on a busy GPU must not
        read as a lost GPU. With no earlier list the node reports no GPUs and the error, which the app reads as a
        check still to come, not as a missing GPU.

        Args:
            quiet: A background re-check: detection's lines are logged at DEBUG unless the GPUs (or the error) changed,
                so an idle node's log isn't the same few lines every few minutes.

        Returns:
            The JSON-safe GPU list now reported.
        """
        with self._detect_lock:
            detect = self._detect
            error: str | None = None
            held: list[tuple[str, str]] = []
            try:
                if detect is None:
                    from ...gpu import detect as gpu_detect

                    detect = gpu_detect.detect_all_gpus
                with nodelog.held_lines(held) if quiet else contextlib.nullcontext():
                    raw = detect()
                found = [_gpu_entry(entry) for entry in raw or []]
            except Exception as exc:  # noqa: BLE001 - a broken probe must not take /v1/info down with it
                logger.opt(exception=True).warning("GPU detection failed on this node; it keeps its last GPU list")
                with self._cache_lock:
                    found = list(self._gpus) if self._gpus is not None else []
                error = f"{type(exc).__name__}: {exc}"
            with self._cache_lock:
                changed = (found, error) != (self._gpus, self._gpu_error)
                self._gpus, self._checked_at, self._gpu_error = found, time.time(), error
            self._detected.set()
            for level, message in held:
                logger.log(level if changed else "DEBUG", "{}", message)
            return [dict(entry) for entry in found]

    def gpus(self) -> list[dict[str, Any]]:
        """The cached GPU list, with ``credit_text`` where known.

        Before any detection has answered, it waits for the background loop's first pass (one detection, not a second
        of its own after it), or detects now when no loop is running.
        """
        thread = self._thread
        if thread is not None:
            while not self._detected.wait(1.0) and thread.is_alive():
                pass
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

    def _note_temp_problem(self, problem: str | None) -> None:
        """Log the temp folder's problem when it appears or changes, and when it is gone.

        The app holds a node with one, and the node's own log is where someone setting it up looks first.

        Args:
            problem: What the temp folder's check found, or None when it can be used.
        """
        with self._temp_lock:
            if problem == self._temp_logged:
                return
            self._temp_logged = problem
        if problem:
            logger.warning("{}: the app sends this node no files until that's fixed", problem)
        else:
            logger.info("The node's temp folder {} can be used again", self._temp_root)

    def block(self, *, busy: int, filled: str | None = None) -> dict[str, Any]:
        """The ``worker`` block of /v1/info.

        Args:
            busy: How many tasks are running.
            filled: The temp folder's problem after a task filled it (``TaskTable.filled_problem``), reported when the
                folder can be used otherwise.

        Returns:
            The block.
        """
        gpus = self.gpus()
        ram_total, ram_available = _memory()
        cores = _cores()
        # The load average is the whole host's: beside a node held to fewer cores (--cpus, --cpuset-cpus) it reads as
        # an overload that isn't the node's, and there is no per-container load to show instead.
        load1 = _load1() if cores >= (os.cpu_count() or 1) else None
        # Every start makes a folder there; one that can't would refuse each file the app sends, so the app reads
        # this and sends none.
        temp_problem = probe_temp_folder(self._temp_root) or filled
        self._note_temp_problem(temp_problem)
        try:
            free_bytes: int | None = shutil.disk_usage(self._temp_root).free
        except OSError:
            free_bytes = None
        if self._ffmpeg is None:
            self._ffmpeg = _ffmpeg()
        return {
            "gpus": gpus,
            "cpu": {
                "cores": cores,
                "load1": load1,
                "ram_total": ram_total,
                "ram_available": ram_available,
            },  # fmt: skip
            "temp": {"path": self._temp_root, "free_bytes": free_bytes, "problem": temp_problem},
            "busy": busy,
            "ffmpeg": dict(self._ffmpeg),
            "gpu_checked_at": self._checked_at,
            "gpu_error": self._gpu_error,
        }
