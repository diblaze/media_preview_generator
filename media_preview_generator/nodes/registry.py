"""The app's view of its nodes: who they are, their live state, and how much work each can take (spec §5, §10.2).

Lane B owns the implementation. The names and types below are the binding interface the job code (Lane C) uses.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from .client import NodeClient
from .protocol import PathMapping

STATE_CONNECTING = "connecting"
STATE_READY = "ready"
STATE_OFFLINE = "offline"
STATE_UPDATE_NEEDED = "update_needed"
STATE_KEY_REFUSED = "key_refused"
STATE_ACCESS_PROBLEM = "access_problem"
STATE_GPU_LOST = "gpu_lost"
STATE_DISABLED = "disabled"


@dataclass(frozen=True)
class NodeView:
    """A snapshot of one configured node."""

    id: str
    name: str
    url: str
    enabled: bool
    state: str
    reason: str
    version: str
    roles: tuple[str, ...]
    gpus: tuple[dict, ...]
    cpu_cores: int
    legacy: bool
    last_seen: float | None


@dataclass(frozen=True)
class NodeCapacity:
    """Whether a node's CPU / GPU members may run work now, and the reason shown when they may not."""

    cpu: bool
    gpu: bool
    reason: str


class NodeRegistry:
    """Configured nodes and their live state (implemented by Lane B)."""

    revision: int = 0

    def views(self) -> list[NodeView]:
        raise NotImplementedError

    def view(self, node_id: str) -> NodeView | None:
        raise NotImplementedError

    def client(self, node_id: str) -> NodeClient:
        raise NotImplementedError

    def mappings(self, node_id: str) -> list[PathMapping]:
        raise NotImplementedError

    def capacity(self, node_id: str) -> NodeCapacity:
        raise NotImplementedError

    def report_unavailable(self, node_id: str) -> None:
        raise NotImplementedError

    def report_not_visible(self, node_id: str, app_path: str, reason: str) -> None:
        raise NotImplementedError

    def report_gpu_error(self, node_id: str, device: str) -> None:
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


_registry: NodeRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> NodeRegistry:
    """The process-wide registry."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = NodeRegistry()
        return _registry


def set_registry(registry: NodeRegistry | None) -> None:
    """Replace the process-wide registry (tests, and the app factory)."""
    global _registry
    with _registry_lock:
        _registry = registry
