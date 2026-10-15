"""The Plex database role of a node: the app's database half, running on the machine Plex runs on.

This is the Plex marker agent (the "Plex helper"), folded into the node server. Plex has no API for intro/credits
markers or loudness, so the app writes them into Plex's own database — and SQLite's WAL locks only work between
processes on the same machine. An app that runs elsewhere therefore can't write them at all. This role closes that
gap: run a node beside Plex with Plex's config folder mounted, point the app at it, and the app asks it to run the
very same code it would have run itself (``markers.publishers.plex_db.LocalPlexDb``, ``loudness.plex_db``,
``library_health.plex_rows``).

What it is not: it is not a remote-control API for Plex. Its routes are fixed operations, and:

* the database path comes from this node's own ``PLEX_CONFIG_DIR`` — never from the caller;
* it runs no SQL the caller supplies, and answers nothing else about the library;
* it refuses exactly what the in-process writers refuse: a database that isn't on a local disk *here*, a schema they
  don't know, a missing or duplicated Plex marker tag row (it never creates one), a database that isn't the connected
  Plex's;
* every request needs the shared key, and a caller speaking another agent protocol is refused by name;
* a request naming another node's id (protocol 2) is refused before it runs: the app saved another machine at this
  address, and this one was given the same key and address.

The wire protocol is the agent's (``markers.publishers.plex_remote``: header ``X-Marker-Agent-Protocol``, protocol 1,
and protocol 2, which adds the node id) so every app that already talks to a Plex helper keeps working. ``/v1/health`` belongs to the node server
(``nodes.server.app``), which carries :func:`agent_block` on it while this role is on.

Run it in one process (``nodes.server.wsgi`` serves one): the app's "one writer at a time" guarantee is a lock inside one
process, and the database's own lock proof assumes this process is the only one of ours holding it.

Import-light on purpose: Flask, loguru and the app's ``markers/publishers``, ``loudness`` database and
``library_health.plex_rows`` modules only, so the slim Plex database image (no ffmpeg, no numpy) can run it.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import zlib
from collections.abc import Generator, Iterator, Mapping, Sequence
from typing import Any

from flask import Blueprint, Flask, Response, abort, current_app, g, jsonify, request
from loguru import logger

from ...library_health.plex_rows import LIBRARY_HEALTH_CAPABILITY, query_rows, section_ids_from_json
from ...loudness import plex_db as loudness_db
from ...loudness.guard import LoudnessPlexDb
from ...markers.fs import filesystem_type, is_network_filesystem, mount_of, mount_root
from ...markers.publishers import plex_remote
from ...markers.publishers.base import NEXT_RUN, Capability, PublishError
from ...markers.publishers.plex_chapters import (
    CHAPTER_CAPABILITY,
    LocalChapters,
    target_from_json,
    target_to_json,
)
from ...markers.publishers.plex_db import (
    LocalPlexDb,
    plex_db_path,
    plex_machine_identifier,
    publish_error_from_sqlite,
)
from ..protocol import KIND_PROTOCOL, KIND_RATE_LIMITED, KIND_WRONG_NODE, NODE_UID_HEADER, sendable_key, whole_number
from .app import SERVER_THREADS
from .identity import wrong_key_limit

# This build's version. The app refuses an agent older than its ``plex_remote.MIN_AGENT_VERSION`` and says both
# numbers, so an image left behind on the Plex host is a message, never a wrong write.
AGENT_VERSION = "1.2.0"
# The wire contracts this build implements. Add to it (never replace) while older apps are still in the wild.
PROTOCOLS = [plex_remote.AGENT_PROTOCOL, plex_remote.AGENT_PROTOCOL_NODE_UID]
# Optional operations beyond markers. The app asks for one (``AgentClient.require``) before sending its requests, so
# an older helper is an "update the helper" message, never a request it can't answer.
CAPABILITIES = [CHAPTER_CAPABILITY, loudness_db.LOUDNESS_CAPABILITY, LIBRARY_HEALTH_CAPABILITY]
# A request body is a marker set and one item's parts: kilobytes. Anything larger is refused before it is parsed.
MAX_BODY_BYTES = 2 * 1024 * 1024
# The longest any one request may make this process wait for the database locks, whatever the caller asks for.
MAX_DEADLINE_S = 120.0
# The longest one read-back request keeps reading before answering with what it has.
MAX_BUDGET_S = 120.0
READ_BACK_MAX_ITEMS = 200
# One file's live parts: Plex names a file by a handful of paths (path mappings), never more.
LOUDNESS_MAX_PATHS = 64
MAX_PATH_CHARS = 32768
LOUDNESS_OPERATIONS = frozenset({"file_checks", "check", "streams", "snapshot", "write", "mark"})
DEFAULT_PORT = 9494
# Each request here can wait minutes for Plex's database on one of the server's threads (a nightly Optimize or a big
# scan holds its write lock), so at most half the threads run them: the rest keep the node's health check, the app's
# checks and a worker node's task polls answered. A request that finds them all taken waits for one only while the
# longest-running has run under SLOT_WAIT_S (a burst of quick ones); past that they are stuck on the database, and it
# is answered "full" at once (``plex_remote.NodeFullError``): the app waits and asks again, holding none of ours.
SLOT_WAIT_S = 2.0
# Library Health's rows are packed and sent in pieces of about this many characters (``_streamed_rows``).
ROWS_CHUNK_CHARS = 64 * 1024
# zlib's fastest level: the whole answer is read and packed while Plex's database is held, so speed beats size here.
# A 100,000-episode answer (126 MB) packs to 22 MB.
ROWS_PACK_LEVEL = 1
# How often a space goes out while the rows are read (``_read_then_sent``): well inside the app's shortest wait for a
# byte (``plex_remote.ANSWER_GRACE_S``).
ROWS_KEEPALIVE_S = 1.0

_ON_WINDOWS = os.name == "nt"
# How undo_record_problem tells it runs in a container.
_CONTAINER_MARKERS = ("/.dockerenv", "/run/.containerenv")
_IMAGE_ROOT_FS = frozenset({"overlay", "fuse.fuse-overlayfs"})
# Filesystems in memory: gone at every restart of the container (or the machine).
_MEMORY_FS = frozenset({"tmpfs", "ramfs"})
# The volume Docker (and Podman) make for an image's ``VOLUME`` when no ``-v`` names one: 64 hex characters under the
# engine's ``volumes`` folder. ``docker rm`` + ``docker run`` starts the new container on a new, empty one.
_UNNAMED_VOLUME_ROOT = re.compile(r"(?:^|/)volumes/[0-9a-f]{64}/_data$")
# The folder the last register_plex_db call serves, for info_block() (one node process serves one Plex).
_registered_config_dir = ""


def machine_identifier(config_dir: str) -> str:
    """Plex's own server id, from its preferences beside the database, or "" when it can't be read.

    The app compares it with the machineIdentifier its Plex reports, so a node set up beside the wrong Plex (or a
    different node answering at a node's address) is refused instead of writing another server's database. Read per
    request: it changes when a server is claimed.

    Args:
        config_dir: Plex's config folder as this node sees it.

    Returns:
        The identifier, or "" — which the app treats as "unknown", never as a mismatch.
    """
    return plex_machine_identifier(config_dir)


def _in_container(root_fs: str | None) -> bool:
    # Docker and Podman leave a marker file; an overlay root is some other runtime's image layers. An LXC container
    # has neither, and keeps its root across updates.
    return root_fs in _IMAGE_ROOT_FS or any(os.path.exists(marker) for marker in _CONTAINER_MARKERS)


def undo_record_problem(*, mountinfo_path: str = "/proc/self/mountinfo") -> str:
    """Why the loudness undo record wouldn't survive an update of this node, or "" when it would.

    The record is ``<CONFIG_DIR>/loudness-writes.jsonl`` (``loudness.plex_db.write_log_path``). In a container whose
    ``CONFIG_DIR`` is no mount of its own, it lives in the container's writable layer and goes with the container on
    the next update (and so do the node's id and generated key), whatever storage driver that layer sits on (overlay,
    rootless Podman's fuse-overlayfs, unRAID's btrfs, ZFS). The Windows node keeps it in its own folder under
    ``%LOCALAPPDATA%`` (its entry point sets ``CONFIG_DIR`` to it), and a Linux node outside a container keeps it on
    the machine's own disk; neither needs a volume.

    The plex-db image declares ``VOLUME /config``, so a container started without a volume of its own gets an unnamed
    one there: it is a mount, but the next ``docker rm`` + ``docker run`` leaves it behind for a new, empty one, so it
    is refused like the container's layer. A folder in memory (``--tmpfs /config``) loses it at every restart. And a
    folder the node can't write (a volume mounted read-only, another user's folder) would fail every write on it, so
    that is refused too, before anything is measured.

    Args:
        mountinfo_path: For tests.
    """
    path = loudness_db.write_log_path()
    return _undo_record_lost(os.path.dirname(path), mountinfo_path) or _undo_record_unwritable(path)


def _undo_record_lost(folder: str, mountinfo_path: str) -> str:
    """Why an undo record in ``folder`` would be lost at an update or restart, or "" (see ``undo_record_problem``)."""
    if _ON_WINDOWS:
        if os.environ.get("CONFIG_DIR", "").strip():
            return ""
        return f"Set CONFIG_DIR to this node's own folder: the loudness undo record would go to {folder}."
    real = os.path.realpath(folder)
    found = mount_of(real, mountinfo_path=mountinfo_path)
    if found is None:
        return ""
    if found[1] in _MEMORY_FS:
        return (
            f"{folder} on this Plex database node is in memory ({found[1]}), so the loudness undo record (and the "
            f"node's id) would be lost at every restart. Mount a volume there (-v <name>:{found[0]}). Loudness is not "
            "written until then."
        )
    if found[0] != "/":
        if not _UNNAMED_VOLUME_ROOT.search(mount_root(real, mountinfo_path=mountinfo_path) or ""):
            return ""
        return (
            f"Mount a volume at {folder} on this Plex database node so the loudness undo record survives updates: the "
            f"one there has no name, so an update starts on a new, empty one. Name it (-v <name>:{found[0]}). "
            "Loudness is not written until then."
        )
    if not _in_container(found[1]):
        return ""
    return (
        f"Mount a volume at {folder} on this Plex database node so the loudness undo record survives updates. "
        "Loudness is not written until then."
    )


def _undo_record_unwritable(path: str) -> str:
    """Why the undo record at ``path`` can't be added to, or "" when it can.

    Opened for appending as each write does, without writing anything; while there is none yet, a file is made and
    removed beside it instead, so a check leaves nothing behind.
    """
    folder = os.path.dirname(path)
    try:
        if os.path.exists(path):
            with open(path, "a", encoding="utf-8"):
                pass
        else:
            with tempfile.NamedTemporaryFile(dir=folder or None, prefix=".loudness-writes-check-"):
                pass
    except OSError as exc:
        return (
            f"This Plex database node can't write its loudness undo record in {folder} "
            f"({exc.strerror or type(exc).__name__}). Make that folder writable for the user the node runs as (a "
            "volume mounted read-only can't be). Loudness is not written until then."
        )
    return ""


def agent_block() -> dict:
    """The agent's ``{"version", "protocols", "capabilities"}``: on every answer here, and on ``/v1/health``."""
    return {"version": AGENT_VERSION, "protocols": list(PROTOCOLS), "capabilities": list(CAPABILITIES)}


class RequestSlots:
    """The role's requests running now, held under a limit (see ``SLOT_WAIT_S``)."""

    def __init__(self) -> None:
        self._changed = threading.Condition()
        self._started: dict[object, float] = {}

    def take(self, limit: int | None) -> object | None:
        """A slot for one request, or None when the node is full.

        Args:
            limit: How many may run at once; None: no limit.

        Returns:
            The slot to give back once the request is answered, or None.
        """
        with self._changed:
            while limit is not None and len(self._started) >= limit:
                left = min(self._started.values()) + SLOT_WAIT_S - time.monotonic()
                if left <= 0:
                    return None
                self._changed.wait(left)
            slot = object()
            self._started[slot] = time.monotonic()
            return slot

    def give_back(self, slot: object) -> None:
        """Free a slot :meth:`take` handed out."""
        with self._changed:
            self._started.pop(slot, None)
            self._changed.notify()


def slot_limit(threads: Any) -> int | None:
    """How many of this role's requests may run at once on a server with ``threads`` threads (None: not known)."""
    return max(1, threads // 2) if type(threads) is int and threads > 0 else None


def info_block(plex_config_dir: str | None = None) -> dict:
    """The role's part of the node's ``/v1/info``: which folder it serves, whether the database is there, which Plex.

    Args:
        plex_config_dir: Plex's config folder; default: the one :func:`register_plex_db` was given.

    Returns:
        ``{"config_dir", "db_present", "machine_identifier"}``.
    """
    folder = _registered_config_dir if plex_config_dir is None else plex_config_dir
    return {
        "config_dir": folder,
        "db_present": bool(folder) and os.path.isfile(plex_db_path(folder)),
        "machine_identifier": machine_identifier(folder) if folder else "",
    }


def _not_on_a_local_disk(db_path: str, mountinfo_path: str) -> str:
    """Why SQLite can't read the database at ``db_path`` safely here, or "" when it can.

    Windows has no mountinfo: there the drive type decides, as ``LocalPlexDb.file_checks`` does.
    """
    if _ON_WINDOWS:
        from . import winplatform

        if winplatform.is_local_drive(db_path):
            return ""
        fs_type: str | None = winplatform.filesystem_label(db_path)
    else:
        fs_type = filesystem_type(db_path, mountinfo_path=mountinfo_path)
        if not is_network_filesystem(fs_type):
            return ""
    where = "a network share" if is_network_filesystem(fs_type) else "a drive that isn't a local disk"
    return f"Plex's database is on {where} ({fs_type}) on this node; it has to be a local disk on the Plex machine"


def _ok(result: dict) -> Any:
    return jsonify({"ok": True, "agent": agent_block(), "result": result})


def _refused(exc: PublishError) -> Any:
    # 409: the role worked, the database said no. The app rebuilds this exact exception (plex_remote).
    return jsonify({"ok": False, "agent": agent_block(), "error": plex_remote.error_to_json(exc)}), 409


def _bad_request(message: str) -> Any:
    return jsonify({"ok": False, "agent": agent_block(), "error": {"kind": "request", "message": message}}), 400


def _streamed_rows(queries: Mapping[str, Iterator[Sequence[Any]]]) -> Generator[str, None, None]:
    """Library Health's answer (``{"ok", "agent", "result": {"rows", "capability"}}``), in pieces as rows are read.

    The capability comes last: a read that fails partway, after the answer's status went out, ends the answer
    without it, which the app reads as an answer it can't use rather than as a library with fewer files. A failure
    SQLite reports goes in the answer as its ``error`` too, so the app tells it as the refusal it is (a busy database
    is tried again on the next run).
    """

    def dumps(value: Any) -> str:
        return json.dumps(value, separators=(",", ":"))

    out = [dumps({"ok": True, "agent": agent_block()})[:-1], ',"result":{"rows":{']
    size = 0
    try:
        for number, (key, rows) in enumerate(queries.items()):
            out.append(f"{',' if number else ''}{dumps(key)}:[")
            for index, row in enumerate(rows):
                piece = dumps(list(row))
                out.append(f",{piece}" if index else piece)
                size += len(piece)
                if size >= ROWS_CHUNK_CHARS:
                    yield "".join(out)
                    out, size = [], 0
            out.append("]")
        out.append(f'}},"capability":{dumps(LIBRARY_HEALTH_CAPABILITY)}}}}}')
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.warning("Library Health's rows stopped partway: {}", exc)
        out.append("]}")
        if isinstance(exc, sqlite3.Error):
            out.append(f',"error":{dumps(plex_remote.error_to_json(publish_error_from_sqlite(exc)))}')
        out.append("}}")
    yield "".join(out)


def _read_then_sent(
    queries: Mapping[str, Iterator[Sequence[Any]]], held: contextlib.ExitStack
) -> Generator[bytes, None, None]:
    """Library Health's answer: read whole while ``held`` (the database, the request's slot) is held, then sent.

    Sent as it was read, the answer held Plex's database, its read transaction and the slot until the app had all but
    the last 16 MB of it, which a slow or stalled app (gone from the network without hanging up) made minutes or for
    good, and every check and write on the node waited. So it is kept compressed in memory, one block per piece, and
    let go of ``held`` before its first byte: memory, not a temp file, because a node may run with a read-only
    filesystem.
    """
    packer = zlib.compressobj(ROWS_PACK_LEVEL)
    blocks = []
    size = 0
    keepalive = time.monotonic() + ROWS_KEEPALIVE_S
    pieces = _streamed_rows(queries)
    try:
        for piece in pieces:
            data = piece.encode()
            # A sync flush per piece: each block unpacks to its own piece, never to more of the answer at once.
            blocks.append(packer.compress(data) + packer.flush(zlib.Z_SYNC_FLUSH))
            size += len(data)
            if size > plex_remote.MAX_ANSWER_BYTES:
                # No app reads further (it refuses the answer as too large), so the database read stops here.
                break
            if time.monotonic() >= keepalive:
                # JSON may start with spaces: one now and then keeps the app's wait for the answer's first byte from
                # running out while a big library is read on a slow machine.
                yield b" "
                keepalive = time.monotonic() + ROWS_KEEPALIVE_S
    finally:
        pieces.close()
        held.close()
    unpacker = zlib.decompressobj()
    for block in blocks:
        yield unpacker.decompress(block)


class _ClosingAnswer:
    """An answer that lets go of what it holds (``held``) once it is sent, or once it never will be."""

    def __init__(self, pieces: Generator[bytes, None, None], held: contextlib.ExitStack) -> None:
        self._pieces = pieces
        self._held = held

    def __iter__(self) -> Iterator[bytes]:
        return self._pieces

    def close(self) -> None:
        # The server calls this on the thread that iterated, which is the one that opened the connection (its lock
        # is per thread), whether or not the answer was sent; a generator never started runs no ``finally``.
        try:
            self._pieces.close()
        finally:
            self._held.close()


def _deadline(body: dict, key: str = "deadline_s", limit: float = MAX_DEADLINE_S) -> float:
    try:
        asked = float(body.get(key) or 0.0)
    except (TypeError, ValueError):
        asked = 0.0
    return time.monotonic() + max(0.0, min(asked, limit))


def _whole(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be a whole number")
    return value


def _paths(raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw or len(raw) > LOUDNESS_MAX_PATHS:
        raise ValueError(f"plex_paths must be a list of 1 to {LOUDNESS_MAX_PATHS} paths")
    if any(not isinstance(p, str) or not p or len(p) > MAX_PATH_CHARS for p in raw):
        raise ValueError("plex_paths must be non-empty strings")
    return list(raw)


def _fields(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()):
        raise ValueError("fields must map names to strings")
    return dict(raw)


def _skipped(raw: Any) -> frozenset[tuple[str, int, str]]:
    if not isinstance(raw, list):
        raise ValueError("skipped must be a list")
    keys = set()
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 3:
            raise ValueError("each skipped entry is [part hash, stream index, codec]")
        part_hash, index, codec = entry
        if not isinstance(part_hash, str) or type(index) is not int or not isinstance(codec, str):
            raise ValueError("each skipped entry is [part hash, stream index, codec]")
        keys.add((part_hash, index, codec))
    return frozenset(keys)


def _loudness_args(operation: str, body: dict) -> dict[str, Any]:
    """The validated arguments of one loudness operation (beyond the identity, which the database checks itself).

    Raises:
        ValueError: A field is missing or not what it should be.
    """
    args: dict[str, Any] = {}
    if operation == "streams":
        args["plex_paths"] = _paths(body.get("plex_paths"))
    if operation in ("snapshot", "mark"):
        args["item_id"] = _whole(body.get("metadata_item_id"), "metadata_item_id", minimum=1)
    if operation == "write":
        args["stream_id"] = _whole(body.get("stream_id"), "stream_id", minimum=1)
        args["fields"] = _fields(body.get("fields"))
        raw = body.get("expected")
        args["expected"] = None if raw is None else loudness_db.stream_from_json(raw)
    if operation == "mark":
        raw = body.get("expected")
        args["expected"] = None if raw is None else loudness_db.snapshot_from_json(raw)
        args["skipped"] = _skipped(body.get("skipped", []))
    return args


def _check_key(key: str) -> bytes:
    # The node server always hands over a key: NODE_KEY, AGENT_TOKEN, or the one it generated into node.json and
    # printed once (``identity.load_identity``). Only a caller that builds the role itself can get here without one.
    shared_key = (key or "").strip()
    if not shared_key:
        raise SystemExit(
            "The Plex database role was started without a key. Start it through the node server, which uses NODE_KEY "
            "(or AGENT_TOKEN) or generates a key and prints it once, then paste that key into the app."
        )
    # The app refuses to store a key outside printable ASCII without spaces (``nodes.protocol.sendable_key``), so
    # such a key could never match one the app sends: every request would be refused with a 401 that looks like a
    # typo on the app's side. Said here once, at start, instead.
    if not sendable_key(shared_key):
        raise SystemExit(
            "The shared key must be printable ASCII with no spaces: the app can't send a key with any other character."
        )
    return shared_key.encode("utf-8", "surrogateescape")


def register_plex_db(
    app: Flask,
    *,
    plex_config_dir: str,
    key: str,
    node_uid: str = "",
    mountinfo_path: str = "/proc/self/mountinfo",
) -> None:
    """Add the Plex database role's routes to a node app.

    Every route here sits behind its own guard (the shared key, then the agent protocol header, then the node id the
    app names, then a JSON object body for a POST), scoped to these routes only: the node server's own routes keep
    theirs. ``/v1/health`` is not added — the node server owns it.

    Args:
        app: The node's Flask app.
        plex_config_dir: Plex's config folder as this node sees it. The database path is derived from it and never
            taken from a request.
        key: The key both sides share.
        node_uid: This node's id (its node block's ``id``). A request naming another id is refused; with none, any
            request naming an id is.
        mountinfo_path: For tests.

    Raises:
        SystemExit: No usable key, or no Plex config folder.
    """
    global _registered_config_dir
    folder = (plex_config_dir or "").strip()
    shared_key_bytes = _check_key(key)
    own_uid = (node_uid or "").strip()
    if not folder:
        raise SystemExit("Set PLEX_CONFIG_DIR to Plex's config folder as this container sees it.")
    _registered_config_dir = folder

    database = LocalPlexDb(lambda: plex_db_path(folder), label="agent", mountinfo_path=mountinfo_path)
    chapters = LocalChapters(folder, mountinfo_path=mountinfo_path)
    bp = Blueprint("plex_db", __name__)
    wrong_keys = wrong_key_limit(app)
    slots = RequestSlots()

    def authorised(caller: str) -> bool:
        header = request.headers.get("Authorization", "")
        offered = header[7:].strip() if header.startswith("Bearer ") else ""
        # compare_digest: the same constant-time check the app's own API does for its token (web/auth.py). The
        # BYTES, not the str: werkzeug decodes headers as latin-1, and compare_digest raises TypeError on a str
        # holding anything above U+007F -- a key with one non-ASCII byte would 500 on the auth path, not 401.
        ok = bool(offered) and secrets.compare_digest(offered.encode("utf-8", "surrogateescape"), shared_key_bytes)
        if not ok:
            wrong_keys.note_wrong(caller)
        return ok

    def speaks_our_protocol() -> bool:
        return whole_number(request.headers.get(plex_remote.PROTOCOL_HEADER, "")) in PROTOCOLS

    @bp.before_request
    def _guard() -> Any:
        caller = request.remote_addr or ""
        wait_s = wrong_keys.blocked_for(caller)
        if wait_s:
            error = {"kind": KIND_RATE_LIMITED}
            return jsonify({"ok": False, "agent": agent_block(), "error": error}), 429, {"Retry-After": str(wait_s)}
        if not authorised(caller):
            # No detail: an unauthenticated caller learns nothing about this Plex.
            return jsonify({"ok": False, "agent": agent_block(), "error": {"kind": "auth"}}), 401
        # /v1/ping is exempt from the protocol check on purpose: when the two sides disagree about the protocol,
        # "which version are you?" is the question the operator needs answered.
        if request.path != "/v1/ping" and not speaks_our_protocol():
            return jsonify(
                {
                    "ok": False,
                    "agent": agent_block(),
                    "error": {
                        "kind": "protocol",
                        "message": (
                            f"This Plex database node is version {AGENT_VERSION} and speaks protocol "
                            f"{', '.join(str(p) for p in PROTOCOLS)}; the app asked for "
                            f"{request.headers.get(plex_remote.PROTOCOL_HEADER, 'none')[:32]}."
                        ),
                    },
                }
            ), 409
        named = request.headers.get(NODE_UID_HEADER, "").strip()
        if not named and whole_number(request.headers.get(plex_remote.PROTOCOL_HEADER, "")) == (
            plex_remote.AGENT_PROTOCOL_NODE_UID
        ):
            # The app sends this protocol only with the id it saved (first contact is protocol 1), so the id was
            # dropped on the way, by a proxy: running the request would skip the check that it is meant for this
            # machine.
            message = (
                f"The node's id ({NODE_UID_HEADER}) didn't arrive with this request, so this node ran nothing. If a "
                "proxy sits between the app and this node, let it pass that header."
            )
            error = {"kind": KIND_PROTOCOL, "message": message, "state": Capability.AGENT_UNAVAILABLE.value}
            return jsonify({"ok": False, "agent": agent_block(), "error": error}), 409
        if named and named != own_uid:
            # The app saved another machine at this address (this one was given the same key and address): whatever
            # it asks was meant for that one's Plex. The app checks which machine answers here before it asks again.
            message = "This node isn't the one the app saved at this address: it has another id."
            error = {"kind": KIND_WRONG_NODE, "message": message}
            return jsonify({"ok": False, "agent": agent_block(), "error": error}), 409
        # The node app's own body limit is its business; these routes keep the agent's, before anything is parsed.
        if (request.content_length or 0) > MAX_BODY_BYTES:
            abort(413)
        request.max_content_length = MAX_BODY_BYTES
        try:
            body = request.get_json(silent=True) if request.method == "POST" else {}
        except RecursionError:  # silent=True only covers ValueError; a deeply nested body raises this instead
            body = None
        if not isinstance(body, dict):
            return _bad_request("The request body must be a JSON object")
        if request.path == "/v1/ping":
            return None  # it never opens the database
        limit = slot_limit(current_app.config.get(SERVER_THREADS))
        slot = slots.take(limit)
        if slot is None:
            message = (
                f"Plex's database is busy: {limit} requests on the Plex database node are waiting for it already; "
                f"{NEXT_RUN}"
            )
            answer, status = _refused(plex_remote.NodeFullError(message, state=Capability.UNREACHABLE))
            return answer, status, {"Retry-After": "1"}
        g.plex_db_slot = slot
        return None

    @bp.teardown_request
    def _give_back_slot(_exc: BaseException | None) -> None:
        slot = g.pop("plex_db_slot", None)
        if slot is not None:
            slots.give_back(slot)

    @bp.get("/v1/ping")
    def ping() -> Any:
        """A shell check (`curl -H "Authorization: Bearer $AGENT_TOKEN" .../v1/ping`): the version, which Plex this
        node serves, and whether the database file is even there. It answers whatever protocol the caller speaks,
        so it still works when an app and a node disagree. Optional operations call it first to require their
        capability before sending a request.
        """
        return _ok(
            {"db_present": os.path.isfile(plex_db_path(folder)), "machine_identifier": machine_identifier(folder)}
        )

    @bp.post("/v1/checks/file")
    def checks_file() -> Any:
        """The database file's own checks, run here: found, local disk, writable, held open by Plex."""
        body = request.get_json(silent=True) or {}
        report = database.file_checks(deadline=_deadline(body))
        # The identity travels with the file checks so the app can refuse a node next to a different Plex before
        # it writes anything (``PlexMarkerPublisher._another_plexs_agent``).
        return _ok({"report": plex_remote.report_to_json(report), "machine_identifier": machine_identifier(folder)})

    @bp.post("/v1/checks/db")
    def checks_db() -> Any:
        """The schema, the marker data and Plex's single marker tag row."""
        body = request.get_json(silent=True) or {}
        report = database.db_checks(deadline=_deadline(body))
        return _ok({"report": plex_remote.report_to_json(report)})

    @bp.post("/v1/chapters/<operation>")
    def chapter_operation(operation: str) -> Any:
        """Fixed chapter snapshot/registration requests; no caller paths or SQL are executed."""
        if operation not in {"check", "read", "register"}:
            return _bad_request("Unknown chapter operation")
        body = request.get_json(silent=True) or {}
        machine, version = body.get("machine_identifier"), body.get("pms_version")
        if not isinstance(machine, str) or not machine or not isinstance(version, str) or not version:
            return _bad_request("A positive server identity and PMS version are required")
        deadline = _deadline(body)
        try:
            result: dict = {"capability": CHAPTER_CAPABILITY}
            if operation == "check":
                result["report"] = plex_remote.report_to_json(chapters.capability(machine, version, deadline=deadline))
            elif operation == "read":
                source, hint = body.get("source_path"), body.get("item_id_hint")
                if not isinstance(source, str) or not source or len(source) > MAX_PATH_CHARS:
                    raise ValueError("A source path is required")
                if hint is not None and (type(hint) is not int or hint < 1):
                    raise ValueError("item_id_hint must be a positive whole number")
                result["target"] = target_to_json(
                    chapters.read(source, machine, version, item_id_hint=hint, deadline=deadline)
                )
            else:
                target = target_from_json(body.get("target"))
                if target.machine_identifier != machine:
                    raise ValueError("Snapshot belongs to another server")
                raw = body.get("revisions")
                if not isinstance(raw, list) or not raw or len(raw) > len(target.chapters):
                    raise ValueError("Expected revisions for the chapters that have an image")
                revisions = {}
                for revision in raw:
                    index = revision["index"]
                    if type(index) is not int or index < 1 or index in revisions:
                        raise ValueError("Invalid or duplicated chapter index")
                    revisions[index] = revision["sha256"]
                chapters.register(target, revisions, version, deadline=deadline)
                result["registered"] = True
            return _ok(result)
        except (KeyError, TypeError, ValueError) as exc:
            return _bad_request(str(exc))
        except PublishError as exc:
            return _refused(exc)

    @bp.post("/v1/item/read")
    def item_read() -> Any:
        """One item's live parts."""
        body = request.get_json(silent=True) or {}
        rating_key = body.get("rating_key")
        if not isinstance(rating_key, int) or isinstance(rating_key, bool) or rating_key < 0:
            return _bad_request("rating_key must be a whole number")
        try:
            item = database.read_item(rating_key, deadline=_deadline(body))
        except PublishError as exc:
            return _refused(exc)
        return _ok({"item": plex_remote.item_read_to_json(item)})

    @bp.post("/v1/item/write")
    def item_write() -> Any:
        """Write one item's markers, in one transaction here."""
        body = request.get_json(silent=True) or {}
        try:
            write = plex_remote.write_request_from_json(body.get("write"))
        except ValueError as exc:
            return _bad_request(str(exc))
        try:
            result = database.write_item(write, deadline=_deadline(body))
        except PublishError as exc:
            return _refused(exc)
        logger.info(
            "item {}: {} ({} marker(s) ours)", write.rating_key, "changed" if result.changed else "no change",
            len(result.ours),
        )  # fmt: skip
        return _ok({"write": plex_remote.write_result_to_json(result)})

    @bp.post("/v1/items/shown")
    def items_shown() -> Any:
        """Read items back: what each shows of what the app left on it."""
        body = request.get_json(silent=True) or {}
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > READ_BACK_MAX_ITEMS:
            return _bad_request(f"items must be a list of at most {READ_BACK_MAX_ITEMS} entries")
        try:
            asks = [plex_remote.shown_ask_from_json(item) for item in raw_items]
        except ValueError as exc:
            return _bad_request(str(exc))
        try:
            timeout_s = max(0.0, min(float(body.get("timeout_s") or 0.0), MAX_DEADLINE_S))
        except (TypeError, ValueError):
            return _bad_request("timeout_s must be a number")
        budget = _deadline(body, "budget_s", MAX_BUDGET_S)
        batch = database.shown_many(asks, timeout_s=timeout_s, cancel_check=lambda: time.monotonic() >= budget)
        return _ok({"batch": plex_remote.shown_batch_to_json(batch)})

    @bp.post("/v1/item/exists")
    def item_exists() -> Any:
        """Whether this Plex still has an item with that rating key."""
        body = request.get_json(silent=True) or {}
        rating_key = body.get("rating_key")
        if not isinstance(rating_key, int) or isinstance(rating_key, bool) or rating_key < 0:
            return _bad_request("rating_key must be a whole number")
        try:
            exists = database.item_exists(rating_key, deadline=_deadline(body))
        except PublishError as exc:
            return _refused(exc)
        return _ok({"exists": exists})

    @bp.post("/v1/loudness/<operation>")
    def loudness_operation(operation: str) -> Any:
        """Loudness reads and writes, run with the app's own functions (``loudness.plex_db``) on a database proven
        to belong to the Plex whose identity the app sends. Writes compare against the snapshot the app took before
        analysis, inside the write's transaction.

        Unlike the in-process writer, the cancel and the source-file check can't run here while the write waits for
        Plex's lock: the app checks both before each attempt and sends short deadlines, retrying while the database
        is busy (``loudness.guard.RemoteLoudnessDb``), so a cancel or a changed file stops the write within one wait.
        """
        if operation not in LOUDNESS_OPERATIONS:
            return _bad_request("Unknown loudness operation")
        body = request.get_json(silent=True) or {}
        deadline = _deadline(body)
        # Passed through as sent: LoudnessPlexDb refuses a missing or mismatched identity the same way in-process.
        identity = (body.get("machine_identifier"), body.get("pms_version"))
        problem = "" if operation in {"streams", "snapshot"} else undo_record_problem(mountinfo_path=mountinfo_path)
        if problem and operation in {"write", "mark"}:
            return _refused(PublishError(problem, state=Capability.MISCONFIGURED))
        loudness = LoudnessPlexDb(
            folder, label="node", identity=lambda: identity, mountinfo_path=mountinfo_path, undo_problem=problem
        )
        result: dict = {"capability": loudness_db.LOUDNESS_CAPABILITY}
        try:
            args = _loudness_args(operation, body)
        except ValueError as exc:
            return _bad_request(str(exc))
        try:
            if operation == "file_checks":
                result["report"] = plex_remote.report_to_json(loudness.file_checks(deadline=deadline))
            elif operation == "check":
                result["report"] = plex_remote.report_to_json(loudness.capability(deadline=deadline))
            elif operation == "streams":
                streams, in_plex = loudness_db.read_streams(loudness, args["plex_paths"], deadline=deadline)
                result["streams"] = [loudness_db.stream_to_json(s) for s in streams]
                result["in_plex"] = in_plex
            elif operation == "snapshot":
                result["snapshot"] = loudness_db.snapshot_to_json(
                    loudness_db.read_item_snapshot(loudness, args["item_id"], deadline=deadline)
                )
            elif operation == "write":
                result["written"] = loudness_db.write_stream(
                    loudness, args["stream_id"], args["fields"], deadline=deadline, expected=args["expected"]
                )
                logger.info(
                    "loudness: stream {}: {}", args["stream_id"], "written" if result["written"] else "had loudness"
                )
            else:
                result["marked"] = loudness_db.mark_item(
                    loudness, args["item_id"], deadline=deadline, expected=args["expected"], skipped=args["skipped"]
                )
                logger.info(
                    "loudness: item {}: {}", args["item_id"], "marked analysed" if result["marked"] else "not marked"
                )
        except PublishError as exc:
            return _refused(exc)
        return _ok(result)

    @bp.post("/v1/library-health/rows")
    def library_health_rows() -> Any:
        """Library Health's fixed queries (``library_health.plex_rows``) for the sections the app names.

        The app sends the ``machine_identifier`` its Plex reports; a node beside a different Plex refuses rather than
        answer with another server's rows. An identifier either side can't read ("") is allowed, as on every route.
        """
        body = request.get_json(silent=True) or {}
        asked = body.get("machine_identifier", "")
        if not isinstance(asked, str):
            return _bad_request("machine_identifier must be a string")
        here = machine_identifier(folder)
        if asked and here and asked != here:
            return _refused(
                PublishError(
                    "This Plex database node is beside a different Plex than the server the app asked about. Point "
                    "the server at the node beside its own Plex.",
                    state=Capability.MISCONFIGURED,
                )
            )
        try:
            section_ids = section_ids_from_json(body.get("section_ids"))
            want_markers, want_chapters = body.get("want_markers", False), body.get("want_chapters", False)
            if type(want_markers) is not bool or type(want_chapters) is not bool:
                raise ValueError("want_markers and want_chapters must be true or false")
        except ValueError as exc:
            return _bad_request(str(exc))
        problem = _not_on_a_local_disk(os.path.realpath(plex_db_path(folder)), mountinfo_path)
        if problem:
            return _refused(PublishError(problem, state=Capability.NEEDS_LOCAL_DB))
        held = contextlib.ExitStack()
        try:
            conn = held.enter_context(database._database(read_only=True, deadline=_deadline(body)))
            queries = query_rows(conn, section_ids, want_markers=want_markers, want_chapters=want_chapters)
            # Opening the file reads nothing: a locked or damaged database shows first at the first row, read here
            # so it is refused as on every other route, not found after the answer's 200 went out.
            first = list(itertools.islice(queries["parts"], 1))
        except PublishError as exc:
            held.close()
            return _refused(exc)
        except sqlite3.Error as exc:
            held.close()
            return _refused(publish_error_from_sqlite(exc))
        queries["parts"] = itertools.chain(first, queries["parts"])
        # The rest is read in one read of the database once the 200 is out, kept compressed: a big library's rows
        # (and their JSON) held as they are took a node past its memory limit. The connection and this request's
        # slot are let go once the read is done, or when the app hangs up before.
        slot = g.pop("plex_db_slot", None)
        if slot is not None:
            held.callback(slots.give_back, slot)
        return Response(_ClosingAnswer(_read_then_sent(queries, held), held), mimetype="application/json")

    app.register_blueprint(bp)
    logger.info("Plex database role {} ready; Plex database: {}", AGENT_VERSION, plex_db_path(folder))


__all__ = [
    "AGENT_VERSION",
    "CAPABILITIES",
    "PROTOCOLS",
    "agent_block",
    "info_block",
    "machine_identifier",
    "register_plex_db",
    "undo_record_problem",
]
