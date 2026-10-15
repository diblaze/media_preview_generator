"""The node's task table: each task is one op run on one file, in its own thread, polled by the app.

A task lives only in this process's memory (gunicorn runs one worker for that reason). The app polls it about once a
second; one nobody has polled for ``unpolled_expiry_s`` is cancelled and its temp dir removed, so an app that
restarted mid-task never leaves FFmpeg running here. A finished task is kept ``finished_keep_s`` after its last poll
so the app can still fetch the artifact, unless the app deletes it first.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from loguru import logger

from ..protocol import (
    CLIENT_TASK_ID_FIELD,
    CLIENT_TASK_ID_PATTERN,
    KIND_CANCELLED,
    KIND_FAILED,
    KIND_FILE_NOT_VISIBLE,
    KIND_GPU_FAILED,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_FILE_NOT_VISIBLE,
    STATUS_RUNNING,
    TASK_FINISHED_KEEP_S,
    TASK_UNPOLLED_EXPIRY_S,
    OpError,
    OpFn,
    PathMapping,
)
from . import access, nodelog

TASK_LOG_LINES = 2000
TASK_DIR_PREFIX = "mpg-task-"
REAP_INTERVAL_S = 5.0
# A DELETE for a task id the node doesn't know yet can overtake its start (the app gave up waiting for the
# start's answer): the id is refused for a start this long after, and this many such ids are remembered.
DELETED_ID_KEEP_S = 300.0
DELETED_IDS_MAX = 1024
_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})

# The app's own "cancelled" exceptions, by module: an op that stops because cancel_check() fired raises one of these
# from deep inside the app's code. Looked up in sys.modules rather than imported, so the table never loads FFmpeg,
# numpy or ONNX code itself (an op that raised one has already loaded its module).
_CANCELLATION_CLASSES = (
    ("media_preview_generator.processing.generator", "CancellationError"),
    ("media_preview_generator.loudness.analyze", "LoudnessCancelled"),
    ("media_preview_generator.markers.credits.frames", "DecodeCancelledError"),
    ("media_preview_generator.markers.credits.textdet_helper", "TextDetCancelledError"),
)


class TaskSpecError(ValueError):
    """The task spec is malformed (answered 400, kind ``request``)."""


class TaskDeletedError(Exception):
    """The app deleted this task id before its start arrived (answered 409, kind ``task_deleted``): nothing runs."""


class TaskStartError(Exception):
    """The node can't take the task (its temp folder is full or can't be written): answered 400, kind ``failed``,
    so the app fails the file with this message instead of reading a 500 as a node that didn't answer."""


def default_temp_root() -> str:
    """``NODE_TEMP_DIR``, or ``<system temp>/mpg-node``."""
    return os.environ.get("NODE_TEMP_DIR", "").strip() or os.path.join(tempfile.gettempdir(), "mpg-node")


def _is_cancellation(exc: BaseException) -> bool:
    for module_name, class_name in _CANCELLATION_CLASSES:
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module is not None else None
        if isinstance(cls, type) and isinstance(exc, cls):
            return True
    return False


def _json_value(value: object) -> object:
    """``value`` as JSON can carry it: lists and dicts kept (the app reads ``args`` and ``fallbacks`` as lists),
    non-finite floats as None, anything else as its text."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def optional_int(spec: dict, field: str) -> int | None:
    """``spec[field]`` as a non-negative int, or None when absent.

    Raises:
        TaskSpecError: Present but not a whole number.
    """
    value = spec.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskSpecError(f"{field} must be a whole number or null")
    return value


def parse_mappings(raw: object) -> list[PathMapping]:
    """The spec's ``mapping`` rows.

    Raises:
        TaskSpecError: Not a list of ``{"app_prefix", "node_prefix"}`` objects.
    """
    if not isinstance(raw, list):
        raise TaskSpecError("mapping must be a list")
    try:
        return [PathMapping.from_json(row) for row in raw]
    except ValueError as exc:
        raise TaskSpecError(str(exc)) from exc


def _client_task_id(spec: dict) -> str | None:
    value = spec.get(CLIENT_TASK_ID_FIELD)
    if value is None:
        return None
    # It names the task in URLs and its temp dir, so only plain id characters.
    if not isinstance(value, str) or not re.fullmatch(CLIENT_TASK_ID_PATTERN, value):
        raise TaskSpecError(f"{CLIENT_TASK_ID_FIELD} must be 16-64 letters, digits, '-' or '_'")
    return value


def _parse_gpu(raw: object) -> tuple[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or not isinstance(raw.get("device"), str):
        raise TaskSpecError('gpu must be {"type", "device"} or null')
    return raw["type"], raw["device"]


class NodeTask:
    """One task: the :class:`~media_preview_generator.nodes.protocol.TaskContext` its op receives, and its state."""

    def __init__(self, task_id: str, spec: dict, mappings: list[PathMapping], gpu: tuple[str, str] | None,
                 temp_dir: str) -> None:  # fmt: skip
        self.id = task_id
        self.op: str = spec["op"]
        self.app_path: str = spec["path"]
        self.size = optional_int(spec, "size")
        self.mtime_ns = optional_int(spec, "mtime_ns")
        self.params: dict = spec.get("params") or {}
        self.mappings = mappings
        self.gpu = gpu
        self.temp_dir = temp_dir
        self.status = STATUS_RUNNING
        self.result: dict | None = None
        self.error: dict | None = None
        self.started = time.monotonic()
        self.finished: float | None = None
        self.last_contact = self.started
        self.discarded = False
        self._progress: dict[str, Any] = {"percent": 0.0}
        self._log: deque[dict[str, Any]] = deque(maxlen=TASK_LOG_LINES)
        self._next_seq = 0
        self._artifact: str | None = None
        self._cancel = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()

    # TaskContext ------------------------------------------------------------------------------------------------

    def cancel_check(self) -> bool:
        return self._cancel.is_set()

    def pause_check(self) -> bool:
        # A cancelled task never reads as paused: an op waiting out a pause would otherwise never see the cancel.
        return self._paused.is_set() and not self._cancel.is_set()

    def progress(self, percent: float, **extra: object) -> None:
        try:
            value = float(percent)
        except (TypeError, ValueError):
            value = 0.0
        update = {str(k): _json_value(v) for k, v in extra.items()}
        with self._lock:
            self._progress = {**self._progress, **update, "percent": value if math.isfinite(value) else 0.0}

    def log(self, level: str, message: str) -> None:
        name = str(level).upper()
        name = name if name in _LEVELS else "INFO"
        self._add_line(name, str(message))
        logger.bind(**{nodelog.TASK_LOGGED_EXTRA: True}).log(name, "[task {}] {}", self.id[:8], message)

    def set_artifact(self, path: str) -> None:
        real = os.path.realpath(path)
        if os.path.commonpath([real, os.path.realpath(self.temp_dir)]) != os.path.realpath(self.temp_dir):
            raise ValueError("an artifact must be inside the task's temp dir")
        with self._lock:
            self._artifact = real

    @contextmanager
    def capture_thread(self) -> Iterator[None]:
        """Route loguru lines from the calling thread into this task's log while inside.

        The op's own thread is captured already; an op that starts threads of its own wraps their bodies in this.
        """
        with nodelog.capture_thread(self._add_line):
            yield

    # Table side -------------------------------------------------------------------------------------------------

    def _add_line(self, level: str, message: str) -> None:
        with self._lock:
            self._log.append({"seq": self._next_seq, "level": level, "message": message})
            self._next_seq += 1

    def request_cancel(self) -> None:
        self._cancel.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    @property
    def artifact(self) -> str | None:
        with self._lock:
            return self._artifact

    @property
    def is_finished(self) -> bool:
        return self.finished is not None

    def finish(self, status: str, *, result: dict | None = None, error: dict | None = None) -> None:
        with self._lock:
            self.status, self.result, self.error = status, result, error
            self.finished = time.monotonic()

    def to_json(self, log_from: int = 0) -> dict[str, Any]:
        """The protocol's TASK_STATE, with log lines whose ``seq`` is ``log_from`` or later."""
        with self._lock:
            lines = [dict(line) for line in self._log if line["seq"] >= log_from]
            end = self.finished if self.finished is not None else time.monotonic()
            return {
                "id": self.id,
                "op": self.op,
                "status": self.status,
                "progress": dict(self._progress),
                "log": lines,
                "next_log": self._next_seq,
                "result": self.result,
                "error": self.error,
                "seconds": round(end - self.started, 2),
            }


class TaskTable:
    """Starts, tracks and expires tasks.

    Args:
        ops: Op name -> function.
        temp_root: Each task's temp dir is made under it.
        unpolled_expiry_s: A running task nobody has contacted for this long is cancelled and dropped.
        finished_keep_s: A finished task is dropped this long after its last contact.
        reap_interval_s: How often the reaper looks.
        on_gpu_failed: Called when a task ends with error kind ``gpu_failed`` (GPU detection re-checks).
    """

    def __init__(
        self,
        ops: Mapping[str, OpFn],
        *,
        temp_root: str,
        unpolled_expiry_s: float = TASK_UNPOLLED_EXPIRY_S,
        finished_keep_s: float = TASK_FINISHED_KEEP_S,
        reap_interval_s: float = REAP_INTERVAL_S,
        on_gpu_failed: Callable[[], None] | None = None,
    ) -> None:
        self._ops = dict(ops)
        self.temp_root = temp_root
        self._unpolled_expiry_s = unpolled_expiry_s
        self._finished_keep_s = finished_keep_s
        self._on_gpu_failed = on_gpu_failed
        self._tasks: dict[str, NodeTask] = {}
        # Task id -> when a DELETE found no such task (oldest first), so a start arriving after it never runs.
        self._deleted_unknown: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        os.makedirs(temp_root, exist_ok=True)
        self._remove_leftovers()
        self._reaper = threading.Thread(
            target=self._reap_loop, args=(reap_interval_s,), name="node-task-reaper", daemon=True
        )
        self._reaper.start()

    @property
    def ops(self) -> list[str]:
        return sorted(self._ops)

    def _remove_leftovers(self) -> None:
        # Tasks live in memory only, so a task dir found at start belongs to a process that died mid-task.
        try:
            names = os.listdir(self.temp_root)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.temp_root, name)
            if name.startswith(TASK_DIR_PREFIX) and os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)

    def start(self, spec: object) -> str:
        """Validate a TASK_SPEC and start its op in a thread.

        Args:
            spec: The request body.

        A spec carrying ``client_task_id`` gets that as its id; a repeated start with the same id answers the task
        already there (running or finished) and starts nothing, and a start whose id was deleted before it arrived
        starts nothing either.

        Returns:
            The task id (the op is still running).

        Raises:
            TaskSpecError: The spec is malformed or names an op this node doesn't have.
            TaskDeletedError: The app deleted this id in the last ``DELETED_ID_KEEP_S``.
            TaskStartError: The task's temp folder can't be made.
        """
        if not isinstance(spec, dict):
            raise TaskSpecError("the task spec must be an object")
        op = spec.get("op")
        if not isinstance(op, str) or op not in self._ops:
            raise TaskSpecError(f"this node has no op {op!r}" if isinstance(op, str) else "op must be a string")
        if not isinstance(spec.get("path"), str) or not spec["path"]:
            raise TaskSpecError("path must be the app's path")
        if spec.get("params") is not None and not isinstance(spec["params"], dict):
            raise TaskSpecError("params must be an object")
        optional_int(spec, "size")
        optional_int(spec, "mtime_ns")
        mappings = parse_mappings(spec.get("mapping"))
        gpu = _parse_gpu(spec.get("gpu"))
        task_id = _client_task_id(spec) or uuid.uuid4().hex
        with self._lock:
            self._refuse_deleted(task_id)
            if task_id in self._tasks:
                return task_id
        temp_dir = self._make_temp_dir(task_id)
        task = NodeTask(task_id, spec, mappings, gpu, temp_dir)
        with self._lock:
            try:
                self._refuse_deleted(task_id)  # its DELETE may have come while the temp dir was made
            except TaskDeletedError:
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise
            if task_id in self._tasks:  # The same start, twice at once: the first one runs.
                shutil.rmtree(temp_dir, ignore_errors=True)
                return task_id
            self._tasks[task_id] = task
        threading.Thread(target=self._run, args=(task,), name=f"node-task-{task_id[:8]}", daemon=True).start()
        return task_id

    def _make_temp_dir(self, task_id: str) -> str:
        """The task's own temp dir, the temp root made again first (a host's tmp cleaner may have removed it).

        Raises:
            TaskStartError: The folder can't be made (a full disk, no permission).
        """
        try:
            os.makedirs(self.temp_root, exist_ok=True)
            return tempfile.mkdtemp(prefix=f"{TASK_DIR_PREFIX}{task_id[:8]}-", dir=self.temp_root)
        except OSError as exc:
            why = exc.strerror or str(exc) or type(exc).__name__
            if exc.errno == errno.ENOSPC:
                message = f"The node's temp folder {self.temp_root} is full ({why})"
            else:
                message = f"The node couldn't make a folder for the task in its temp folder {self.temp_root} ({why})"
            logger.warning("Task {} not started: {}", task_id[:8], message)
            raise TaskStartError(message) from exc

    def _run(self, task: NodeTask) -> None:
        try:
            with task.capture_thread():
                self._run_op(task)
        except BaseException as exc:
            logger.opt(exception=exc).error("Task {} ({}) crashed the node's task runner", task.id[:8], task.op)
            raise
        finally:
            if not task.is_finished:
                task.finish(STATUS_FAILED, error={"kind": KIND_FAILED, "message": "The node's task runner failed"})
            with self._lock:
                discarded = task.discarded
            if discarded:
                shutil.rmtree(task.temp_dir, ignore_errors=True)
            if task.error and task.error.get("kind") == KIND_GPU_FAILED and self._on_gpu_failed is not None:
                try:
                    self._on_gpu_failed()
                except Exception:  # noqa: BLE001 - a re-check failing must not take the task thread's report with it
                    logger.opt(exception=True).warning("Couldn't schedule a GPU re-check after task {}", task.id[:8])

    def _run_op(self, task: NodeTask) -> None:
        quiet = logger.bind(**{nodelog.TASK_LOGGED_EXTRA: True})
        try:
            node_path = access.resolve(task.app_path, task.size, task.mtime_ns, task.mappings)
        except access.NotVisible as exc:
            quiet.info("Task {} ({}): file not visible: {}", task.id[:8], task.op, exc.message)
            task.finish(
                STATUS_FILE_NOT_VISIBLE,
                error={"kind": KIND_FILE_NOT_VISIBLE, "reason": exc.reason, "message": exc.message},
            )
            return
        quiet.info("Task {} ({}) started on {}", task.id[:8], task.op, node_path)
        try:
            result = self._ops[task.op](task, node_path, task.params)
        except OpError as exc:
            if exc.kind == KIND_CANCELLED:
                task.finish(STATUS_CANCELLED, error=exc.to_json())
            else:
                quiet.info("Task {} ({}) ended: {}: {}", task.id[:8], task.op, exc.kind, exc.message)
                task.finish(STATUS_FAILED, error=exc.to_json())
            return
        except Exception as exc:  # noqa: BLE001 - every failure must end as a task status the app can read
            if task.cancel_check() or _is_cancellation(exc):
                task.finish(STATUS_CANCELLED, error={"kind": KIND_CANCELLED, "message": "Cancelled"})
                return
            quiet.opt(exception=exc).error("Task {} ({}) failed", task.id[:8], task.op)
            task.finish(STATUS_FAILED, error={"kind": KIND_FAILED, "message": f"{type(exc).__name__}: {exc}"})
            return
        if task.cancel_check():
            task.finish(STATUS_CANCELLED, error={"kind": KIND_CANCELLED, "message": "Cancelled"})
            return
        try:
            if not isinstance(result, dict):
                raise TypeError(f"the op returned {type(result).__name__}, not a dict")
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            quiet.error("Task {} ({}) returned a result that isn't JSON: {}", task.id[:8], task.op, exc)
            task.finish(STATUS_FAILED, error={"kind": KIND_FAILED, "message": f"{type(exc).__name__}: {exc}"})
            return
        quiet.info("Task {} ({}) done", task.id[:8], task.op)
        task.finish(STATUS_DONE, result=result)

    def _touch(self, task_id: str) -> NodeTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.last_contact = time.monotonic()
            return task

    def poll(self, task_id: str, log_from: int = 0) -> dict[str, Any] | None:
        """The task's state (None: unknown), counting as contact."""
        task = self._touch(task_id)
        return task.to_json(log_from) if task is not None else None

    def cancel(self, task_id: str) -> bool:
        """Ask the op to stop. False: unknown task."""
        task = self._touch(task_id)
        if task is None:
            return False
        task.request_cancel()
        return True

    def set_paused(self, task_id: str, paused: bool) -> bool:
        """Pause or resume the task (its op's ``pause_check()``). False: unknown task."""
        task = self._touch(task_id)
        if task is None:
            return False
        task.set_paused(paused)
        return True

    def get(self, task_id: str) -> NodeTask | None:
        """The task (None: unknown), counting as contact."""
        return self._touch(task_id)

    def _refuse_deleted(self, task_id: str) -> None:
        # Under the lock. Ids past their time go first; the dict is in deletion order.
        now = time.monotonic()
        while self._deleted_unknown:
            oldest, deleted_at = next(iter(self._deleted_unknown.items()))
            if now - deleted_at < DELETED_ID_KEEP_S:
                break
            del self._deleted_unknown[oldest]
        if task_id in self._deleted_unknown:
            raise TaskDeletedError("The app deleted this task before its start arrived; nothing was started")

    def delete(self, task_id: str) -> bool:
        """Drop the task now: cancel it if it runs, free its temp dir. False: unknown task, whose id is then refused
        for a start for ``DELETED_ID_KEEP_S`` (the DELETE overtook it)."""
        with self._lock:
            task = self._tasks.pop(task_id, None)
            if task is None:
                self._deleted_unknown.pop(task_id, None)
                self._deleted_unknown[task_id] = time.monotonic()
                while len(self._deleted_unknown) > DELETED_IDS_MAX:
                    self._deleted_unknown.popitem(last=False)
        if task is None:
            return False
        self._discard(task)
        return True

    def __contains__(self, task_id: object) -> bool:
        """Whether the table knows the task, without counting as contact."""
        with self._lock:
            return task_id in self._tasks

    def busy(self) -> int:
        """How many tasks are running."""
        with self._lock:
            return sum(1 for task in self._tasks.values() if not task.is_finished)

    def _discard(self, task: NodeTask) -> None:
        # A running op may still be writing into its temp dir; its own thread removes the dir once it returns.
        with self._lock:
            task.discarded = True
            finished = task.is_finished
        task.request_cancel()
        if finished:
            shutil.rmtree(task.temp_dir, ignore_errors=True)

    def reap(self) -> None:
        """One pass: drop running tasks nobody polls and finished tasks past their keep time."""
        now = time.monotonic()
        expired: list[tuple[NodeTask, str]] = []
        with self._lock:
            for task_id, task in list(self._tasks.items()):
                if task.is_finished:
                    if now - max(task.last_contact, task.finished or 0.0) >= self._finished_keep_s:
                        expired.append((self._tasks.pop(task_id), "kept long enough after its last poll"))
                elif now - task.last_contact >= self._unpolled_expiry_s:
                    expired.append((self._tasks.pop(task_id), "nobody polled it, so it was cancelled"))
        for task, why in expired:
            logger.info("Task {} ({}) removed: {}", task.id[:8], task.op, why)
            self._discard(task)

    def _reap_loop(self, interval_s: float) -> None:
        while not self._stop.wait(interval_s):
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - the reaper must outlive one bad pass
                logger.opt(exception=True).warning("The node's task reaper failed one pass")

    def close(self) -> None:
        """Stop the reaper and drop every task (tests; a node process just exits)."""
        self._stop.set()
        with self._lock:
            tasks, self._tasks = list(self._tasks.values()), {}
        for task in tasks:
            self._discard(task)
