"""The wire contract between the app and a node — the one module both sides import.

A node is another machine that runs heavy steps for the app (the worker role) or opens Plex's database where Plex
runs (the Plex database role). Spec: ``docs/design/remote-nodes/README.md`` §3. Everything here is plain data: no
Flask, no requests, nothing that touches media, so a node's slim builds can import it too.

The Plex database role keeps the Plex marker agent's own protocol (``markers.publishers.plex_remote``), so helpers
already deployed keep working; the constants below cover the node routes, and the node id that protocol's version 2
adds (``NODE_UID_HEADER``).
"""

from __future__ import annotations

import base64
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import numpy as np

# The node protocol this build speaks. Bump only for an incompatible change, and keep answering the old one while
# apps that speak it are still in the wild.
NODE_PROTOCOL = 1
PROTOCOL_HEADER = "X-MPG-Node-Protocol"
# Sent on every answer of the app's web side (never a node's), with an id per running app: an address entered in
# Add node that answers with it is this app, or another copy of it, not a node.
APP_INSTANCE_HEADER = "X-MPG-App-Instance"
# Sent on each Plex database request once the app has saved the node's id (the node block's ``id``): a node with
# another id refuses it (409 ``wrong_node``) before running anything, so another machine given the same key and
# address never writes into its Plex what was meant for this node's (agent protocol 2, ``plex_remote``).
NODE_UID_HEADER = "X-MPG-Node-Uid"

DEFAULT_PORT = 9494

ROLE_WORKER = "worker"
ROLE_PLEX_DB = "plex_db"
ROLES = (ROLE_WORKER, ROLE_PLEX_DB)


def node_version(info: dict) -> str:
    """The version a node is shown and judged by, from its ``/v1/info``.

    A worker runs this app's own code, so its package version is the one that has to match. A node with only the Plex
    database role is held to that role's version (``info["agent"]["version"]``, the Plex helper's numbering that the
    docs and the minimum version use); its package version is the app release it was built from, or ``0.0.0.dev0`` for
    a local build, and says nothing about what it can do.

    Args:
        info: The node's ``/v1/info`` answer.

    Returns:
        The version, or "" when the node sent none.
    """
    block = info.get("node") if isinstance(info.get("node"), dict) else {}
    roles = block.get("roles") if isinstance(block.get("roles"), list) else []
    agent = info.get("agent") if isinstance(info.get("agent"), dict) else {}
    if ROLE_PLEX_DB in roles and ROLE_WORKER not in roles and agent.get("version"):
        return str(agent["version"])
    return str(block.get("version") or "")


def build_sha() -> str:
    """This build's git commit: the image's ``GIT_SHA``, or "" when the build wasn't given one.

    Images built without a release version (a pull request's, a local ``docker build``) all report the same version,
    so the commit is what tells a stale worker node from a current one.
    """
    value = os.environ.get("GIT_SHA", "").strip().lower()
    return value if re.fullmatch(r"[0-9a-f]{7,40}", value) else ""


def same_build(node_build: str, app_build: str) -> bool:
    """Whether two commits are the same build; True when either is unknown.

    One side may report the short commit and the other the full one, so a prefix of at least 7 characters matches.
    """
    a, b = str(node_build or "").strip().lower(), str(app_build or "").strip().lower()
    if not a or not b:
        return True
    if min(len(a), len(b)) < 7:
        return a == b
    return a.startswith(b) or b.startswith(a)


def sendable_key(key: str) -> bool:
    """Whether a key can go in an Authorization header: printable ASCII with no spaces (33..126).

    A pasted curly quote or zero-width space would make every request fail with the key in a traceback, and the
    node's constant-time compare refuses non-ASCII. Both sides apply this one rule: the app refuses to store such a
    key, and a node refuses to start with one, since no request the app sends could ever match it.
    """
    return bool(key) and all(33 <= ord(ch) <= 126 for ch in key)


# The longest key either side takes: the app refuses to store a longer one, so a node refuses to start with one.
KEY_MAX_LEN = 200


def whole_number(raw: str) -> int | None:
    """``raw`` as a non-negative int when it is 1 to 18 ASCII digits (surrounding spaces ignored), else None.

    ``str.isdigit()`` alone takes "²", which ``int()`` refuses, and ``int()`` refuses text of over 4,300 digits: a
    request with either must be refused as malformed, not answered with a 500.
    """
    text = str(raw).strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) <= 18 else None


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
# The node's temp folder filled up, went read-only or refused the node's user mid-task: the node's condition, not the
# file's.
KIND_TEMP_FOLDER = "temp_folder"
KIND_AUTH = "auth"
KIND_PROTOCOL = "protocol"
KIND_UNKNOWN_TASK = "unknown_task"
KIND_REQUEST = "request"
# A start whose client_task_id the app already DELETEd (the DELETE overtook the start): nothing ran.
KIND_TASK_DELETED = "task_deleted"
# A start that reached a node already stopping (503): nothing ran; /v1/health and /v1/info say ``"stopping": true``.
KIND_NODE_STOPPING = "node_stopping"
# Too many wrong keys came from the caller's address (429, with Retry-After): its key wasn't compared, nothing ran.
KIND_RATE_LIMITED = "rate_limited"
# A Plex database request naming another node's id (``NODE_UID_HEADER``, 409): the machine at the address isn't the
# one the app saved there; nothing ran.
KIND_WRONG_NODE = "wrong_node"

# Why a node can't see a file (``error.reason`` / access-check ``kind``).
NOT_VISIBLE_UNMAPPED = "unmapped"
NOT_VISIBLE_OUTSIDE_ROOTS = "outside_roots"
NOT_VISIBLE_MISSING = "missing"
NOT_VISIBLE_DIFFERENT_FILE = "different_file"
NOT_VISIBLE_UNREADABLE = "unreadable"
# The same reasons in plain words, for job logs and the Nodes page.
NOT_VISIBLE_TEXT = {
    NOT_VISIBLE_UNMAPPED: "no folder mapping covers it",
    NOT_VISIBLE_OUTSIDE_ROOTS: "it is outside the folders the node may read",
    NOT_VISIBLE_MISSING: "not found on the node",
    NOT_VISIBLE_DIFFERENT_FILE: "the node has a different file at that path",
    NOT_VISIBLE_UNREADABLE: "the node can't read it",
}


# Text a node sends that this app logs or shows (its log lines, a task's error message, step text) is cut to this many
# characters (an FFmpeg line is well under it).
RELAY_LINE_MAX = 4096
# Control characters but tab, C1 controls (U+0085 is a line break; U+009B starts a terminal escape like ESC [) and the
# Unicode line and paragraph separators: any of them would add lines to the app's log that read as its own.
_LINE_BREAKING_CHARS = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029]")


def relayed_text(raw: object, limit: int = RELAY_LINE_MAX) -> str:
    """Text a node sent, as one line of at most ``limit`` characters with its control characters escaped.

    Whatever answers at a node's address chooses this text, and the app writes it into its log, the job log and
    the job's file rows, so it is kept to what a real node sends: single lines of reasonable length.

    Args:
        raw: What the node sent; anything that isn't text reads as "".
        limit: The most characters kept.

    Returns:
        The text, cut and escaped.
    """
    text = raw[:limit] if isinstance(raw, str) else ""
    # repr() spells "\n" as the two characters \n and "\x1b" as \x1b.
    return _LINE_BREAKING_CHARS.sub(lambda match: repr(match.group())[1:-1], text)


def not_visible_text(reason: str) -> str:
    """A node's reason code for a file it can't see, in plain words (an unknown code is shown as it is)."""
    return NOT_VISIBLE_TEXT.get(reason, reason or NOT_VISIBLE_TEXT[NOT_VISIBLE_MISSING])


# Ops: the heavy steps a worker node runs.
OP_PREVIEW_FRAMES = "preview.frames"
OP_LOUDNESS_ANALYZE = "loudness.analyze"
OP_INTRO_FINGERPRINT = "intro.fingerprint"
OP_INTRO_END_PICTURE = "intro.end_picture"
OP_CREDITS_FIND = "credits.find"
OPS = (OP_PREVIEW_FRAMES, OP_LOUDNESS_ANALYZE, OP_INTRO_FINGERPRINT, OP_INTRO_END_PICTURE, OP_CREDITS_FIND)

# TASK_SPEC's optional ``client_task_id``: the app's own id for the task, which the node uses as the task id. A
# repeated start with the same id answers the existing task instead of starting another, and the app can cancel a
# task whose start answer it never got (``DELETE /v1/tasks/<client_task_id>``).
CLIENT_TASK_ID_FIELD = "client_task_id"
CLIENT_TASK_ID_PATTERN = r"[A-Za-z0-9_-]{16,64}"

# A task nobody has polled for this long is cancelled and its temp dir removed: an app that restarted mid-task
# never leaves work running on a node.
TASK_UNPOLLED_EXPIRY_S = 60.0
# A finished task's artifact is kept this long after its last poll unless the app deletes it first.
TASK_FINISHED_KEEP_S = 600.0


class TaskContext(Protocol):
    """What a node hands each op (spec §10.1). The node server's task table implements it."""

    gpu: tuple[str, str] | None
    temp_dir: str

    def cancel_check(self) -> bool: ...

    def pause_check(self) -> bool: ...

    def progress(self, percent: float, **extra: object) -> None: ...

    def log(self, level: str, message: str) -> None: ...

    def set_artifact(self, path: str) -> None: ...


OpFn = Callable[[TaskContext, str, dict], dict]


class OpError(Exception):
    """Raised by an op to finish its task with a specific error kind (``KIND_*``) and extra JSON-safe fields."""

    def __init__(self, kind: str, message: str, **extra: object) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.extra = extra

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "message": self.message, **self.extra}


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
