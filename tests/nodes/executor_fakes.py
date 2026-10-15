"""Fakes for remote execution tests: a node registry, a scripted node client, a loopback node and a task context."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from typing import Any

from media_preview_generator.nodes import protocol
from media_preview_generator.nodes.client import UNREACHABLE_TIMEOUT, NodeUnreachableError
from media_preview_generator.nodes.protocol import OpError, PathMapping
from media_preview_generator.nodes.registry import NodeRegistry, NodeView

NODE_ID = "node-a"
NODE_NAME = "NAS"
GPUS = ({"type": "NVIDIA", "device": "cuda:0", "name": "RTX 3060", "status": "ok"},)


class FakeRegistry(NodeRegistry):
    """Clients by node id, identity mappings, and every report made."""

    def __init__(self, clients: dict[str, Any], *, gpus: tuple[dict, ...] = GPUS) -> None:
        self.clients = clients
        self.gpus = gpus
        self.reports: list[tuple] = []

    def view(self, node_id: str) -> NodeView | None:
        if node_id not in self.clients:
            return None
        return NodeView(node_id, node_id, "http://n", True, "ready", "", "1", ("worker",), self.gpus, 8, False, None)

    def client(self, node_id: str):
        return self.clients[node_id]

    def mappings(self, node_id: str) -> list[PathMapping]:
        if node_id not in self.clients:
            raise KeyError(node_id)
        return [PathMapping("/data", "/mnt/media")]

    def report_unavailable(self, node_id: str) -> None:
        self.reports.append(("unavailable", node_id))

    def report_not_visible(self, node_id: str, app_path: str, reason: str) -> None:
        self.reports.append(("not_visible", node_id, app_path, reason))

    def report_gpu_error(self, node_id: str, device: str) -> None:
        self.reports.append(("gpu_error", node_id, device))


def running(**extra: Any) -> dict:
    return {"id": "t1", "status": protocol.STATUS_RUNNING, "progress": {"percent": 0.0}, "log": [], **extra}


def finished(status: str, *, result: dict | None = None, error: dict | None = None, **extra: Any) -> dict:
    return {"id": "t1", "status": status, "result": result, "error": error, "log": [], **extra}


class ScriptedClient:
    """Answers polls from a script: dicts, exceptions to raise, or callables of the client giving either."""

    def __init__(self, script: list, *, artifact: bytes = b"", start_error: Exception | None = None) -> None:
        self.script = list(script)
        self.artifact = artifact
        self.start_error = start_error
        self.calls: list[tuple] = []
        self.spec: dict | None = None
        self.started_with: dict | None = None
        self.deleted: list[str] = []

    def start_task(self, spec: dict) -> str:
        self.calls.append(("start",))
        self.started_with = spec
        if self.start_error is not None:
            raise self.start_error
        self.spec = spec
        return "t1"

    def poll_task(self, task_id: str, *, log_from: int = 0) -> dict:
        self.calls.append(("poll", log_from))
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if callable(step):
            step = step(self)
        if isinstance(step, BaseException):
            raise step
        return step

    def cancel_task(self, task_id: str) -> None:
        self.calls.append(("cancel",))

    def pause_task(self, task_id: str) -> None:
        self.calls.append(("pause",))

    def resume_task(self, task_id: str) -> None:
        self.calls.append(("resume",))

    def fetch_artifact(self, task_id: str) -> bytes:
        self.calls.append(("artifact",))
        return self.artifact

    def delete_task(self, task_id: str, *, read_timeout: float | None = None) -> None:
        self.calls.append(("delete",))
        self.deleted.append(task_id)

    def named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


def unreachable() -> NodeUnreachableError:
    """What the client raises for a connect timeout: the connection was never made."""
    return NodeUnreachableError("http://n didn't answer in time", reason=UNREACHABLE_TIMEOUT)


class FakeTaskContext:
    """What the node's task table hands an op."""

    def __init__(self, temp_dir: str, gpu: tuple[str, str] | None = None) -> None:
        self.gpu = gpu
        self.temp_dir = temp_dir
        self.cancelled = False
        self.paused = False
        self.progress_calls: list[dict] = []
        self.logs: list[tuple[str, str]] = []
        self.artifact: str | None = None

    def cancel_check(self) -> bool:
        return self.cancelled

    def pause_check(self) -> bool:
        return self.paused

    def progress(self, percent: float, **extra: object) -> None:
        self.progress_calls.append({"percent": percent, **extra})

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))

    def set_artifact(self, path: str) -> None:
        self.artifact = path


class LoopbackClient:
    """A node in this process: runs ``ops.OPS`` on the app's own path when the task starts, answers polls with the
    finished state. The mapping is checked (it must be the identity the test registry gives for ``/``)."""

    def __init__(self, gpu: tuple[str, str] | None = None, *, map_path: Callable[[str], str] = lambda p: p) -> None:
        self.gpu = gpu
        self.map_path = map_path
        self.state: dict = {}
        self.ctx: FakeTaskContext | None = None
        self.specs: list[dict] = []

    def start_task(self, spec: dict) -> str:
        from media_preview_generator.nodes.server.ops import OPS

        self.specs.append(spec)
        self.ctx = ctx = FakeTaskContext(tempfile.mkdtemp(prefix="loopback-"), self.gpu)
        try:
            result = OPS[spec["op"]](ctx, self.map_path(spec["path"]), spec["params"])
            state = finished(protocol.STATUS_DONE, result=result)
        except OpError as exc:
            status = {
                protocol.KIND_CANCELLED: protocol.STATUS_CANCELLED,
                protocol.KIND_FILE_NOT_VISIBLE: protocol.STATUS_FILE_NOT_VISIBLE,
            }.get(exc.kind, protocol.STATUS_FAILED)
            state = finished(status, error=exc.to_json())
        except Exception as exc:  # the task table's own catch-all
            state = finished(protocol.STATUS_FAILED, error={"kind": protocol.KIND_FAILED, "message": str(exc)})
        state["progress"] = ctx.progress_calls[-1] if ctx.progress_calls else {"percent": 0.0}
        self.state = state
        return "t1"

    def poll_task(self, task_id: str, *, log_from: int = 0) -> dict:
        return self.state

    def cancel_task(self, task_id: str) -> None:
        pass

    def pause_task(self, task_id: str) -> None:
        pass

    def resume_task(self, task_id: str) -> None:
        pass

    def fetch_artifact(self, task_id: str) -> bytes:
        assert self.ctx is not None and self.ctx.artifact is not None
        with open(self.ctx.artifact, "rb") as fh:
            return fh.read()

    def delete_task(self, task_id: str, *, read_timeout: float | None = None) -> None:
        pass


def identity_registry(client: Any, gpus: tuple[dict, ...] = GPUS) -> FakeRegistry:
    """A registry whose one node sees the app's paths as they are."""
    registry = FakeRegistry({NODE_ID: client}, gpus=gpus)
    registry.mappings = lambda node_id: [PathMapping("/", "/")]  # type: ignore[method-assign]
    return registry


def write_file(path: str, data: bytes = b"x") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path
