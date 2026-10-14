"""What the node registry says about worker group members that run on a remote node (spec §5, §10.2).

The worker pool reads one ``NodeHardware`` snapshot per reconcile and keeps it with its targets, so capacity, member
snapshots and new worker slots all agree on the same view of each node until the next reconcile. Policies without a
``node`` never touch the registry.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from loguru import logger

# Settings hold no per-device FFmpeg thread value for a node's GPU (``gpu_config`` describes this app's GPUs), so a
# node GPU worker uses the same default a local GPU gets when ``gpu_config`` has no entry for it.
NODE_GPU_FFMPEG_THREADS = 2

MEMBER_STATE_NODE_UNAVAILABLE = "node_unavailable"

_REASON_NODE_REMOVED = "node removed"
_REASON_NODE_UNKNOWN = "node status unavailable"
_REASON_GPU_MISSING = "GPU not found on this node"

_failed_reads: set[str] = set()


@dataclass(frozen=True)
class NodeMemberHardware:
    """Whether one node member may have workers now, and what its workers run on.

    Attributes:
        available: The node gives this member capacity now.
        reason: Why it has zero workers (empty when available).
        node_name: The node's display name (its id when the node is gone).
        gpu: The node's GPU entry (``type``, ``device``, ``name``, ``status``) for an available GPU member.
        gpu_index: Position of that GPU in the node's list.
    """

    available: bool
    reason: str
    node_name: str
    gpu: dict | None = None
    gpu_index: int | None = None


@dataclass(frozen=True)
class NodeHardware:
    """One read of the registry for the nodes a set of policies uses.

    Attributes:
        revision: ``NodeRegistry.revision`` when read, or ``None`` when no policy uses a node.
        views: Node id -> ``NodeView`` (``None`` for a node the registry no longer knows).
        capacities: Node id -> ``NodeCapacity``.
    """

    revision: int | None = None
    views: dict = field(default_factory=dict)
    capacities: dict = field(default_factory=dict)

    def node_name(self, node_id: str) -> str:
        """The node's display name, falling back to its id."""
        view = self.views.get(node_id)
        return (getattr(view, "name", "") or node_id) if view is not None else node_id

    def node_state(self, node_id: str) -> str | None:
        """The node's registry state (``ready``, ``offline``...), or ``None`` when the registry no longer knows it."""
        view = self.views.get(node_id)
        return getattr(view, "state", None) if view is not None else None

    def node_ref(self, node_id: str | None) -> dict | None:
        """``{"id", "name"}`` for a worker status row; ``None`` for this app."""
        return {"id": node_id, "name": self.node_name(node_id)} if node_id else None

    def member(self, policy: dict) -> NodeMemberHardware:
        """Capacity of one node member policy.

        Args:
            policy: A member policy whose ``node`` is set.

        Returns:
            The member's hardware view; a node missing from this snapshot reads as removed.
        """
        node_id = policy["node"]
        name = self.node_name(node_id)
        capacity = self.capacities.get(node_id)
        if capacity is None:
            return NodeMemberHardware(False, _REASON_NODE_REMOVED, name)
        reason = capacity.reason or _REASON_NODE_UNKNOWN
        if policy["resource"] == "cpu":
            return NodeMemberHardware(bool(capacity.cpu), "" if capacity.cpu else reason, name)
        if not capacity.gpu:
            return NodeMemberHardware(False, reason, name)
        view = self.views.get(node_id)
        for index, gpu in enumerate(getattr(view, "gpus", ()) or ()):
            if isinstance(gpu, dict) and gpu.get("device") == policy.get("device") and gpu.get("status") != "failed":
                return NodeMemberHardware(True, "", name, dict(gpu), index)
        return NodeMemberHardware(False, _REASON_GPU_MISSING, name)


EMPTY_NODE_HARDWARE = NodeHardware()


def registry_revision() -> int:
    """The registry's current revision; it bumps on any node state or capacity change."""
    from ..nodes.registry import get_registry

    revision = getattr(get_registry(), "revision", 0)
    return revision if type(revision) is int else 0


def read_node_hardware(policies: Iterable[dict]) -> NodeHardware:
    """Read capacity and views for every node the policies use.

    A node the registry cannot answer for reads as zero capacity with a plain reason, so one broken node never stops
    this app's own workers from reconciling.

    Args:
        policies: Member policies (``member_policies`` output).

    Returns:
        The snapshot; ``EMPTY_NODE_HARDWARE`` when no policy uses a node.
    """
    node_ids = sorted({p["node"] for p in policies if p.get("node")})
    if not node_ids:
        return EMPTY_NODE_HARDWARE
    from ..nodes.registry import NodeCapacity, get_registry

    registry = get_registry()
    revision = getattr(registry, "revision", 0)
    views: dict = {}
    capacities: dict = {}
    for node_id in node_ids:
        try:
            views[node_id] = registry.view(node_id)
            capacities[node_id] = registry.capacity(node_id)
        except Exception as exc:  # noqa: BLE001 - any registry failure must read as "no capacity", not stop reconcile
            if node_id not in _failed_reads:
                logger.warning(
                    "Could not read node {} from the node registry ({}); its members get no workers", node_id, exc
                )
                _failed_reads.add(node_id)
            views[node_id] = None
            capacities[node_id] = NodeCapacity(False, False, _REASON_NODE_UNKNOWN)
        else:
            _failed_reads.discard(node_id)
    return NodeHardware(revision if type(revision) is int else 0, views, capacities)


def member_detected(policy: dict, local_devices: set[str] | dict, nodes: NodeHardware) -> bool:
    """Whether a member's hardware exists now: a local CPU, a detected local GPU, or capacity on its node."""
    if policy.get("node"):
        return nodes.member(policy).available
    return policy["resource"] == "cpu" or policy.get("device") in local_devices


def worker_node_id(worker: object) -> str | None:
    """The node a worker runs on, or ``None`` for this app (also for stand-in workers without the attribute)."""
    node_id = getattr(worker, "node_id", None)
    return node_id if isinstance(node_id, str) and node_id else None
