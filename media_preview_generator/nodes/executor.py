"""Runs one heavy step on a node for a remote worker: start, poll, log lines, cancel, pause, errors (spec §6, §10.3).

A remote worker is an ordinary worker whose thread carries a :class:`Placement` (:func:`placed_on`). Each of the five
call sites that run a heavy step asks :func:`current_placement` and either calls the local function or hands the same
arguments to ``nodes.remote_ops``, which runs the op here with :func:`run_on_node` and turns its answer back into what
the local function returns.

The placement is a ``ContextVar`` set by the worker thread around its file. Every call site runs on that thread (no
pipeline on these paths hands a call site to a pool or a helper thread), so nothing has to copy the context.
"""

from __future__ import annotations

import io
import os
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NamedTuple

from loguru import logger

from ..processing.generator import FALLBACK_CODEC, FALLBACK_HWACCEL, CodecNotSupportedError
from . import protocol
from .client import (
    MAX_ARTIFACT_BYTES,
    NodeClient,
    NodeError,
    NodeRequestError,
    NodeTimeoutError,
    NodeUnknownTaskError,
)
from .registry import NodeRegistry, get_registry

# How often a running task is polled; a cancel or a pause flip is forwarded within one slice of it.
POLL_INTERVAL_S = 1.0
_SLICE_S = 0.2
# A task's node counts as gone once its polls have failed for this long, and at least MAX_FAILED_POLLS of them in a
# row (spec §6). By time, not count alone: a refused connection fails at once, so three polls would be a 2 s blip.
NODE_GONE_AFTER_S = 12.0
MAX_FAILED_POLLS = 3
# A cancelled task the node still reports running after this long is given up on: the node's own unpolled-task expiry
# stops it.
CANCEL_WAIT_S = 30.0
# A preview frame is a 320x240 JPEG (tens of KB; raw RGB would be 225 KB), and a 4-hour film at the 1 s interval
# is 14,400 of them: anything past these is not a preview's frames.
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_FRAMES = 100_000
_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})

REQUEUE_NODE_UNAVAILABLE = "node_unavailable"
# The node never ran the task (it refused the connection or didn't answer the start): no drop counts against the file.
REQUEUE_NODE_NOT_STARTED = "node_not_started"
REQUEUE_NOT_VISIBLE = "not_visible"
# What the "Started" line logged when a node takes a task calls each op.
_OP_LABELS = {
    protocol.OP_PREVIEW_FRAMES: "preview frames",
    protocol.OP_LOUDNESS_ANALYZE: "loudness analysis",
    protocol.OP_INTRO_FINGERPRINT: "season audio fingerprint",
    protocol.OP_INTRO_END_PICTURE: "end-picture frames",
    protocol.OP_CREDITS_FIND: "credit text",
}


@dataclass(frozen=True)
class Placement:
    """Where a worker's heavy steps run: on this node."""

    node_id: str
    node_name: str


_placement: ContextVar[Placement | None] = ContextVar("node_placement", default=None)


def current_placement() -> Placement | None:
    """The current thread's placement; None runs heavy steps locally."""
    return _placement.get()


@contextmanager
def placed_on(placement: Placement | None) -> Iterator[None]:
    """Run the block's heavy steps on ``placement`` (None: locally).

    Args:
        placement: The node, or None.
    """
    token = _placement.set(placement)
    try:
        yield
    finally:
        _placement.reset(token)


class NodeUnavailableError(Exception):
    """The node stopped answering mid-task, or no longer knows the task. Not the file's failure: it is queued again.

    Attributes:
        node_id: The node.
        node_name: Its name.
        started: False when the node never ran the task (it didn't take the start), so the drop doesn't count.
    """

    def __init__(self, node_id: str, node_name: str, message: str, *, started: bool = True) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.node_name = node_name
        self.started = started


class FileNotVisibleError(Exception):
    """The node can't see the file as the app sees it. Not the file's failure: another node (or this app) runs it.

    Attributes:
        node_id: The node.
        node_name: Its name.
        reason: ``protocol.NOT_VISIBLE_*``.
    """

    def __init__(self, node_id: str, node_name: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.node_name = node_name
        self.reason = reason


class NodeTaskError(RuntimeError):
    """The op ran on the node and failed. ``nodes.remote_ops`` raises the local exception it names instead.

    Attributes:
        kind: ``protocol.KIND_*``.
        message: The node's message, as the local exception would carry it.
        error_type: The class name of the exception the op raised on the node ("" when it named none).
        extra: The error's other fields.
    """

    def __init__(self, kind: str, message: str, *, error_type: str = "", extra: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.error_type = error_type
        self.extra = dict(extra or {})


class NodeTaskCancelledError(NodeTaskError):
    """The task was cancelled (the job's cancel, forwarded)."""


class NodeRequeue(NamedTuple):
    """Why a remote worker gave its file back without an outcome; the dispatcher queues it again or fails it."""

    reason: str  # REQUEUE_NODE_UNAVAILABLE, REQUEUE_NODE_NOT_STARTED or REQUEUE_NOT_VISIBLE
    node_id: str
    node_name: str
    message: str


def requeue_for(exc: BaseException) -> NodeRequeue | None:
    """The requeue a worker records for ``exc``; None for any other exception."""
    if isinstance(exc, NodeUnavailableError):
        reason = REQUEUE_NODE_UNAVAILABLE if exc.started else REQUEUE_NODE_NOT_STARTED
        return NodeRequeue(reason, exc.node_id, exc.node_name, str(exc))
    if isinstance(exc, FileNotVisibleError):
        return NodeRequeue(REQUEUE_NOT_VISIBLE, exc.node_id, exc.node_name, str(exc))
    return None


def extract_artifact(data: bytes, dest: str) -> list[str]:
    """Unpack a preview's frames tar into ``dest``: plain files with bare names only, every entry checked first.

    Args:
        data: The tar.
        dest: An existing folder.

    Returns:
        The names unpacked, sorted.

    Raises:
        NodeTaskError: The tar is unreadable, holds anything but plain files with bare names, or is far bigger
            than any preview's frames.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            members = []
            total = 0
            # A sparse member passes isfile() yet unpacks to any size it declares from a few bytes of tar.
            for member in tar:
                name = member.name
                if (
                    not member.isfile()
                    or member.issparse()
                    or not name
                    or name in (".", "..")
                    or "/" in name
                    or "\\" in name
                    or name != os.path.basename(name)
                ):
                    raise NodeTaskError(protocol.KIND_FAILED, f"The node's frames hold an unexpected entry {name!r}")
                total += member.size
                if member.size > MAX_FRAME_BYTES or total > MAX_ARTIFACT_BYTES or len(members) >= MAX_FRAMES:
                    raise NodeTaskError(protocol.KIND_FAILED, "The node's frames are far bigger than a preview's")
                members.append(member)
            tar.extractall(dest, members=members, filter="data")
    except tarfile.TarError as exc:
        raise NodeTaskError(protocol.KIND_FAILED, f"The node's frames couldn't be unpacked: {exc}") from exc
    return sorted(member.name for member in members)


def _device_label(registry: NodeRegistry, node_id: str, gpu: tuple[str, str] | None) -> str:
    """``GPU<n>`` by the device's place in the node's GPU list, or ``CPU``."""
    if gpu is None:
        return "CPU"
    try:
        view = registry.view(node_id)
    except Exception:  # a label must never stop the file
        view = None
    for index, entry in enumerate(view.gpus if view is not None else ()):
        if isinstance(entry, dict) and entry.get("device") == gpu[1]:
            return f"GPU{index}"
    return "GPU"


def placement_label(gpu: tuple[str, str] | None) -> str:
    """``<node>/<GPUn|CPU>`` for the current thread's node, as the job log prefixes that node's lines; "" locally.

    Args:
        gpu: ``(type, device)`` on the node, or None for its CPU.
    """
    placement = current_placement()
    if placement is None:
        return ""
    return f"{placement.node_name}/{_device_label(get_registry(), placement.node_id, gpu)}"


def _report(what: str, fn: Callable[..., None], *args: str) -> None:
    """Tell the registry about the node; its failure must not hide the file's own error."""
    try:
        fn(*args)
    except Exception as exc:
        logger.warning("Couldn't report {} to the node registry: {}: {}", what, type(exc).__name__, exc)


class _TaskRun:
    """One task on one node, driven from the worker's thread."""

    def __init__(
        self,
        registry: NodeRegistry,
        client: NodeClient,
        placement: Placement,
        *,
        gpu: tuple[str, str] | None,
        cancel_check: Callable[[], bool] | None,
        pause_check: Callable[[], bool] | None,
        progress_callback: Callable[[dict], None] | None,
        path_swap: tuple[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._client = client
        self._placement = placement
        self._gpu = gpu
        self._cancel_check = cancel_check
        self._pause_check = pause_check
        self._progress_callback = progress_callback
        self._prefix = f"{placement.node_name}/{_device_label(registry, placement.node_id, gpu)} · "
        self._task_id = ""
        self._next_log = 0
        self._last_progress: dict | None = None
        self._progress_warned = False
        self._cancel_seen_at: float | None = None
        self._cancel_delivered = False
        self._paused_at: float | None = None
        self._forgotten = False
        # (node's path, app's path) of the file: the job log names it by this app's path throughout.
        self._path_swap = path_swap

    def _unavailable(self, message: str, *, started: bool = True) -> Exception:
        _report("an unavailable node", self._registry.report_unavailable, self._placement.node_id)
        if self._cancel_seen_at is not None:
            # The job was cancelled meanwhile: the file ends as cancelled, never queued again.
            return NodeTaskCancelledError(protocol.KIND_CANCELLED, message)
        return NodeUnavailableError(self._placement.node_id, self._placement.node_name, message, started=started)

    def start(self, spec: dict) -> None:
        name = self._placement.node_name
        try:
            self._task_id = self._client.start_task(spec)
        except NodeRequestError as exc:
            raise NodeTaskError(protocol.KIND_REQUEST, f"{name} refused the task: {exc}") from exc
        except NodeTimeoutError as exc:
            # The node may have started it without the answer reaching us: stop it now rather than leave FFmpeg
            # running there beside the file's next try until the node's unpolled-task expiry.
            self._drop_unanswered(spec[protocol.CLIENT_TASK_ID_FIELD])
            raise self._unavailable(f"{name} couldn't start the task: {exc}", started=False) from exc
        except NodeError as exc:
            raise self._unavailable(f"{name} couldn't start the task: {exc}", started=False) from exc
        # Live, as the node takes it: the step's own lines and progress follow under the same prefix.
        logger.info(
            "{}Started {}: {}", self._prefix, _OP_LABELS.get(spec["op"], spec["op"]), os.path.basename(spec["path"])
        )

    def _drop_unanswered(self, client_task_id: str) -> None:
        try:
            self._client.delete_task(client_task_id)
        except NodeUnknownTaskError:
            pass  # It never started.
        except NodeError as exc:
            logger.debug("{}couldn't cancel the unanswered start: {}", self._prefix, exc)

    def _cancel_requested(self) -> bool:
        return bool(self._cancel_check and self._cancel_check())

    def _pause_wanted(self) -> bool:
        return bool(self._pause_check and self._pause_check())

    def _forward_controls(self) -> bool:
        """Send the job's cancel, and each pause flip, to the node; a failed send is tried again next slice.

        Returns:
            True when a cancel or a pause flip reached the node.
        """
        if self._cancel_seen_at is None and self._cancel_requested():
            self._cancel_seen_at = time.monotonic()
        if self._cancel_seen_at is not None:
            if self._cancel_delivered:
                return False
            try:
                self._client.cancel_task(self._task_id)
            except NodeError as exc:
                logger.debug("{}cancel not delivered yet: {}", self._prefix, exc)
                return False
            self._cancel_delivered = True
            return True
        if self._pause_check is None:
            return False
        wanted = self._pause_wanted()
        if wanted == (self._paused_at is not None):
            return False
        try:
            if wanted:
                self._client.pause_task(self._task_id)
                self._paused_at = time.monotonic()
            else:
                self._client.resume_task(self._task_id)
                self._add_held(time.monotonic() - (self._paused_at or time.monotonic()))
                self._paused_at = None
        except NodeError as exc:
            logger.debug("{}pause/resume not delivered yet: {}", self._prefix, exc)
            return False
        return True

    def _add_held(self, seconds: float) -> None:
        # A shared markers Freeze carries the time held, so deadlines the app keeps across steps move out by a pause
        # that happened on the node, as by one held here.
        held = getattr(self._pause_check, "held_s", None)
        if isinstance(held, float):
            self._pause_check.held_s = held + seconds  # type: ignore[union-attr]

    def _controls_changed(self) -> bool:
        if self._cancel_seen_at is not None:
            return not self._cancel_delivered
        if self._cancel_requested():
            return True
        return self._pause_check is not None and (self._pause_wanted() != (self._paused_at is not None))

    def _sleep(self) -> None:
        # A control that reached the node ends the wait so the next poll shows its effect; one that didn't is sent
        # again each slice without polling any faster.
        deadline = time.monotonic() + POLL_INTERVAL_S
        while (left := deadline - time.monotonic()) > 0:
            time.sleep(min(_SLICE_S, left))
            if self._controls_changed() and self._forward_controls():
                return

    def _relay(self, state: dict) -> None:
        for line in state.get("log") or []:
            if not isinstance(line, dict):
                continue
            level = str(line.get("level") or "INFO").upper()
            message = str(line.get("message", ""))
            if self._path_swap:
                message = message.replace(*self._path_swap)
            logger.log(level if level in _LEVELS else "INFO", "{}{}", self._prefix, message)
        next_log = state.get("next_log")
        if isinstance(next_log, int):
            self._next_log = next_log
        progress = state.get("progress")
        if self._progress_callback is None or not isinstance(progress, dict) or progress == self._last_progress:
            return
        self._last_progress = dict(progress)
        try:
            self._progress_callback(progress)
        except Exception as exc:  # progress must never fail a file
            if not self._progress_warned:
                self._progress_warned = True
                logger.warning("{}progress update failed (later failures not logged): {}", self._prefix, exc)

    def wait(self) -> dict:
        """Poll until the task finishes; its last state."""
        name = self._placement.node_name
        failed_polls = 0
        failing_since = 0.0
        while True:
            self._forward_controls()
            asked_at = time.monotonic()
            try:
                state = self._client.poll_task(self._task_id, log_from=self._next_log)
            except NodeUnknownTaskError as exc:
                self._forgotten = True
                raise self._unavailable(f"{name} no longer knows the task (it restarted?)") from exc
            except NodeError as exc:
                if failed_polls == 0:
                    failing_since = asked_at
                failed_polls += 1
                if failed_polls >= MAX_FAILED_POLLS and time.monotonic() - failing_since >= NODE_GONE_AFTER_S:
                    raise self._unavailable(f"{name} stopped answering: {exc}") from exc
                self._sleep()
                continue
            failed_polls = 0
            self._relay(state)
            if state.get("status") in protocol.FINISHED_STATUSES:
                return state
            if self._cancel_seen_at is not None and time.monotonic() - self._cancel_seen_at > CANCEL_WAIT_S:
                raise NodeTaskCancelledError(protocol.KIND_CANCELLED, f"{name} didn't stop the cancelled task")
            self._sleep()

    def finish(self, state: dict, app_path: str, artifact_to: str | None) -> dict:
        """The result of a finished task, or the error it ended with."""
        status = state.get("status")
        raw_error = state.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        kind = str(error.get("kind") or "")
        message = str(error.get("message") or kind or status or "failed")
        node_id, name = self._placement.node_id, self._placement.node_name
        if status == protocol.STATUS_DONE:
            if artifact_to is not None:
                self._download(artifact_to)
            result = state.get("result")
            return result if isinstance(result, dict) else {}
        if status == protocol.STATUS_FILE_NOT_VISIBLE or kind == protocol.KIND_FILE_NOT_VISIBLE:
            reason = str(error.get("reason") or protocol.NOT_VISIBLE_MISSING)
            _report("a file the node can't see", self._registry.report_not_visible, node_id, app_path, reason)
            # The job log names the file by this app's path already; the node's message would add its own path.
            plain = protocol.not_visible_text(reason)
            raise FileNotVisibleError(node_id, name, reason, f"{name} can't see the file: {plain}")
        error_type = str(error.get("error_type") or "")
        extra = {k: v for k, v in error.items() if k not in ("kind", "message", "error_type")}
        if status == protocol.STATUS_CANCELLED or kind == protocol.KIND_CANCELLED:
            raise NodeTaskCancelledError(protocol.KIND_CANCELLED, message, error_type=error_type, extra=extra)
        fallback_kind = str(error.get("fallback_kind") or "")
        if kind == protocol.KIND_CODEC_NOT_SUPPORTED:
            raise CodecNotSupportedError(message, kind=fallback_kind or FALLBACK_CODEC)
        if kind == protocol.KIND_GPU_FAILED:
            device = self._gpu[1] if self._gpu else ""
            _report("a GPU error", self._registry.report_gpu_error, node_id, device)
            if not error_type:
                raise CodecNotSupportedError(message, kind=fallback_kind or FALLBACK_HWACCEL)
        raise NodeTaskError(kind or protocol.KIND_FAILED, message, error_type=error_type, extra=extra)

    def _download(self, dest: str) -> None:
        try:
            data = self._client.fetch_artifact(self._task_id)
        except NodeError as exc:
            raise self._unavailable(f"{self._placement.node_name} didn't send the frames: {exc}") from exc
        extract_artifact(data, dest)

    def delete(self) -> None:
        # Also after the node stopped answering: when it comes back, a task it still runs stops now rather than at
        # its unpolled-task expiry, beside the file's next try.
        if not self._task_id or self._forgotten:
            return
        try:
            self._client.delete_task(self._task_id)
        except NodeError as exc:
            logger.debug("{}couldn't free the task: {}", self._prefix, exc)


def run_on_node(
    op: str,
    app_path: str,
    params: dict,
    *,
    gpu: tuple[str, str] | None,
    cancel_check: Callable[[], bool] | None,
    pause_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    artifact_to: str | None = None,
) -> dict:
    """Run ``op`` on the current thread's node and return its result.

    The node's log lines go into this thread's log (so the job's log) at their level, prefixed
    ``<node>/<GPUn|CPU> · ``; the job's cancel and pause are forwarded to the task.

    Args:
        op: ``protocol.OP_*``.
        app_path: The file as the app sees it; the node maps it and checks it is the same file (size + mtime_ns).
        params: The op's plain parameters.
        gpu: ``(type, device)`` on the node, or None for its CPU.
        cancel_check: True once the job is cancelled.
        pause_check: True while the file's work must stop where it is.
        progress_callback: Called with the task's ``progress`` dict (``{"percent", ...}``) each time it changes;
            ``nodes.remote_ops`` turns it into the local function's own callback.
        artifact_to: A folder to unpack the task's artifact into (a preview's frames).

    Returns:
        The op's result.

    Raises:
        NodeUnavailableError: The node didn't start the task (``started`` False), stopped answering, or forgot it.
        FileNotVisibleError: The node can't see the file as the app sees it.
        CodecNotSupportedError: The node's GPU couldn't run the op (the worker reruns it on the node's CPU).
        NodeTaskCancelledError: The job was cancelled.
        NodeTaskError: The op failed on the node.
        OSError: The app can't stat ``app_path``.
        RuntimeError: No placement is set.
    """
    placement = current_placement()
    if placement is None:
        raise RuntimeError("run_on_node needs a placement (placed_on)")
    registry = get_registry()
    stat = os.stat(app_path)
    try:
        client = registry.client(placement.node_id)
        mappings = registry.mappings(placement.node_id)
    except KeyError as exc:
        raise NodeUnavailableError(
            placement.node_id, placement.node_name, f"{placement.node_name} is no longer configured", started=False
        ) from exc
    spec = {
        protocol.CLIENT_TASK_ID_FIELD: uuid.uuid4().hex,
        "op": op,
        "path": app_path,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mapping": [m.to_json() for m in mappings],
        "gpu": None if gpu is None else {"type": gpu[0], "device": gpu[1]},
        "params": params,
    }
    node_path = protocol.map_app_path(app_path, mappings)
    run = _TaskRun(
        registry,
        client,
        placement,
        gpu=gpu,
        cancel_check=cancel_check,
        pause_check=pause_check,
        progress_callback=progress_callback,
        path_swap=(node_path, app_path) if node_path and node_path != app_path else None,
    )
    run.start(spec)
    try:
        return run.finish(run.wait(), app_path, artifact_to)
    finally:
        run.delete()
