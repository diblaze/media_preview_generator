"""The wire contract between the app and a node — the one module both sides import.

A node is another machine that runs heavy steps for the app (the worker role) or opens Plex's database where Plex
runs (the Plex database role). Spec: ``docs/design/remote-nodes/README.md`` §3. Everything here is plain data: no
Flask, no requests, nothing that touches media, so a node's slim builds can import it too.

The Plex database role keeps the Plex marker agent's own protocol (``markers.publishers.plex_remote``) unchanged, so
helpers already deployed keep working; the constants below cover the node routes only.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

# The node protocol this build speaks. Bump only for an incompatible change, and keep answering the old one while
# apps that speak it are still in the wild.
NODE_PROTOCOL = 1
PROTOCOL_HEADER = "X-MPG-Node-Protocol"

DEFAULT_PORT = 9494

ROLE_WORKER = "worker"
ROLE_PLEX_DB = "plex_db"
ROLES = (ROLE_WORKER, ROLE_PLEX_DB)

# Routes, so neither side spells them twice.
ROUTE_HEALTH = "/v1/health"
ROUTE_INFO = "/v1/info"
ROUTE_GPUS_RESCAN = "/v1/gpus/rescan"
ROUTE_ACCESS_CHECK = "/v1/access-check"
ROUTE_TASKS = "/v1/tasks"
ROUTE_LOG = "/v1/log"

# Task statuses.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_FILE_NOT_VISIBLE = "file_not_visible"
FINISHED_STATUSES = frozenset({STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED, STATUS_FILE_NOT_VISIBLE})

# Error kinds. The first group can come back on a task; the second on any request.
KIND_FILE_NOT_VISIBLE = "file_not_visible"
KIND_CODEC_NOT_SUPPORTED = "codec_not_supported"
KIND_GPU_FAILED = "gpu_failed"
KIND_CANCELLED = "cancelled"
KIND_FAILED = "failed"
KIND_AUTH = "auth"
KIND_PROTOCOL = "protocol"
KIND_UNKNOWN_TASK = "unknown_task"
KIND_REQUEST = "request"

# Why a node can't see a file (``error.reason`` / access-check ``kind``).
NOT_VISIBLE_UNMAPPED = "unmapped"
NOT_VISIBLE_OUTSIDE_ROOTS = "outside_roots"
NOT_VISIBLE_MISSING = "missing"
NOT_VISIBLE_DIFFERENT_FILE = "different_file"
NOT_VISIBLE_UNREADABLE = "unreadable"

# Ops: the heavy steps a worker node runs.
OP_PREVIEW_FRAMES = "preview.frames"
OP_LOUDNESS_ANALYZE = "loudness.analyze"
OP_INTRO_FINGERPRINT = "intro.fingerprint"
OP_INTRO_END_PICTURE = "intro.end_picture"
OP_CREDITS_FIND = "credits.find"
OPS = (OP_PREVIEW_FRAMES, OP_LOUDNESS_ANALYZE, OP_INTRO_FINGERPRINT, OP_INTRO_END_PICTURE, OP_CREDITS_FIND)

# A task nobody has polled for this long is cancelled and its temp dir removed: an app that restarted mid-task
# never leaves work running on a node.
TASK_UNPOLLED_EXPIRY_S = 60.0
# A finished task's artifact is kept this long after its last poll unless the app deletes it first.
TASK_FINISHED_KEEP_S = 600.0


@dataclass(frozen=True)
class PathMapping:
    """One mapping row: the app sees media under ``app_prefix``, the node under ``node_prefix``."""

    app_prefix: str
    node_prefix: str

    def to_json(self) -> dict[str, str]:
        return {"app_prefix": self.app_prefix, "node_prefix": self.node_prefix}

    @classmethod
    def from_json(cls, raw: Any) -> PathMapping:
        if not isinstance(raw, dict):
            raise ValueError("a mapping row must be an object")
        app_prefix, node_prefix = raw.get("app_prefix"), raw.get("node_prefix")
        if not isinstance(app_prefix, str) or not app_prefix or not isinstance(node_prefix, str) or not node_prefix:
            raise ValueError("a mapping row needs app_prefix and node_prefix")
        return cls(app_prefix, node_prefix)


def _prefix_matches(path: str, prefix: str, sep: str) -> bool:
    trimmed = prefix.rstrip("/\\") or prefix
    return path == trimmed or path.startswith(trimmed + sep) or (trimmed.endswith(sep) and path.startswith(trimmed))


def map_app_path(path: str, mappings: list[PathMapping], *, node_sep: str = "/") -> str | None:
    """The node's path for an app path, by the longest matching ``app_prefix``; None when no row covers it.

    The app's paths are always POSIX (the app runs in Linux Docker). ``node_sep`` is the node's separator, so a
    Windows node gets ``D:\\Media\\…`` back.

    Args:
        path: The app's path.
        mappings: The node's mapping rows.
        node_sep: The node's path separator.

    Returns:
        The mapped path, or None.
    """
    best: PathMapping | None = None
    for row in mappings:
        if _prefix_matches(path, row.app_prefix, "/") and (best is None or len(row.app_prefix) > len(best.app_prefix)):
            best = row
    if best is None:
        return None
    rest = path[len(best.app_prefix.rstrip("/")) :].lstrip("/")
    base = best.node_prefix.rstrip("/\\") if len(best.node_prefix) > 1 else best.node_prefix
    if not rest:
        return base
    if node_sep != "/":
        rest = rest.replace("/", node_sep)
    return base + (node_sep if not base.endswith(node_sep) else "") + rest


def encode_array(array: np.ndarray, dtype: str) -> dict[str, Any]:
    """A numpy array as JSON: base64 of its little-endian bytes, plus dtype and shape."""
    import numpy as np  # Local: the slim Plex database builds carry no numpy and never call this.

    data = np.ascontiguousarray(array, dtype=np.dtype(dtype).newbyteorder("<"))
    return {"dtype": dtype, "shape": list(data.shape), "b64": base64.b64encode(data.tobytes()).decode("ascii")}


def decode_array(raw: dict[str, Any]) -> np.ndarray:
    """The inverse of :func:`encode_array`, in native byte order."""
    import numpy as np

    dtype = np.dtype(raw["dtype"]).newbyteorder("<")
    data = np.frombuffer(base64.b64decode(raw["b64"]), dtype=dtype).reshape(raw["shape"])
    return data.astype(np.dtype(raw["dtype"]), copy=True)
