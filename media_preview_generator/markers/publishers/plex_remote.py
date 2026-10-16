"""The Plex marker agent's wire protocol, and the transport that speaks it.

Plex's database can only be written from the machine Plex runs on (SQLite's WAL locks are not shared over a network
filesystem), so an app on another machine has no way in. The **Plex marker agent** is a small container the user runs
beside Plex with Plex's config folder mounted: it runs ``plex_db.LocalPlexDb`` there, and this module is how the app
asks it to. Both sides use the codec below, so a remote write is the same code as a local one — the agent is a
transport for arguments, never a second implementation of the rules.

What the agent will not do: it takes no database path from the app (its own ``PLEX_CONFIG_DIR`` decides), it exposes
no SQL, and it refuses everything ``LocalPlexDb`` refuses (a database that isn't on a local disk **on its side**, a
schema it doesn't know, a missing or duplicated marker tag row).
"""

from __future__ import annotations

import codecs
import json
import re
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import requests
from loguru import logger

from ...nodes.client import answer_pieces, new_session
from ...nodes.protocol import KIND_PROTOCOL, KIND_WRONG_NODE, NODE_UID_HEADER, relayed_text
from ..decide import FileLimits
from ..models import Marker, MarkerType
from .base import (
    Capability,
    CapabilityReport,
    DatabaseBusyError,
    ItemNotFoundError,
    PublishError,
    Shown,
    wait_cancelled,
)
from .plex_db import (
    ItemRead,
    LocalPlexDb,
    Part,
    PlexDatabase,
    ShownAnswer,
    ShownAsk,
    ShownBatch,
    SiblingResult,
    SiblingState,
    WriteRequest,
    WriteResult,
    report_of_failure,
)

# The wire contract. Bumped only when a request or answer changes shape; an agent says which protocols it implements
# and refuses the others, so a skew in either direction is a refusal with both versions named, never a wrong write.
AGENT_PROTOCOL = 1
# Protocol 1 plus the node id: each request names the node this app saved at the address (``NODE_UID_HEADER``) and a
# node with another id refuses it. Sent only once that id is saved, so an old Plex helper (it has none) keeps
# protocol 1, and a node from before the check refuses protocol 2 outright instead of running a request meant for
# another machine.
AGENT_PROTOCOL_NODE_UID = 2
PROTOCOL_HEADER = "X-Marker-Agent-Protocol"
# The oldest agent build this app works with. The agent's version is its own (its image tag), not the app's.
MIN_AGENT_VERSION = "1.0.0"
# A version or protocol list a node sends is a few characters; it is cut to this before it is quoted.
AGENT_FIELD_MAX = 200
# Seconds allowed for the TCP connect, and for the agent to answer after the deadline it was given has passed.
CONNECT_TIMEOUT_S = 4.0
ANSWER_GRACE_S = 3.0
# The read timeout is the longest wait for the next byte, so an answer sent a byte at a time never ran out of it. Each
# answer must arrive whole within the connect timeout and the time the request allows, counted from the request on.
# Library Health's rows get this much more: the node reads them all after its wait for the database, then sends them.
# Measured on 100,000 episodes: a 105 MB answer, read in 4.8 s and sent in 0.4 s here. A machine ten times slower on a
# 10 Mbit/s link takes ~2 minutes.
ROWS_ANSWER_S = 600.0
# One read-back request: at most this many items, and the agent stops reading after this long and answers with what
# it has (the items left are simply unread, exactly as a cancelled local read-back leaves them).
READ_BACK_CHUNK = 50
READ_BACK_BUDGET_S = 60.0
# How long this app waits before asking a full Plex database node again (``NodeFullError``).
NODE_FULL_RETRY_S = 0.5
# The body fields that say how long the node may wait for the database; a request asked again sends what is left.
_WAIT_FIELDS = ("deadline_s", "budget_s")
# The most of one answer this app reads: Library Health's rows, the largest answer (~2 KB per media part measured on
# a lab library of 778 parts), so 512 MB fits ~250,000 parts. Answers are never compressed, so a small reply can't
# expand into gigabytes here.
MAX_ANSWER_BYTES = 512 * 1024 * 1024
# Library Health's rows are parsed as they arrive and each is counted at what it takes in memory here (``_RowsReader``):
# JSON of tiny rows (``[],``) parses into ~25x its size, so the bytes alone don't bound it. Measured: 100,000 episodes'
# rows take 248 MB (their 105 MB answer peaked at 336 MB read whole, 234 MB now), so this fits ~430,000.
MAX_ROWS_MEMORY_BYTES = 1024 * 1024 * 1024
# Every other answer: JSON parses into ~25x its size here (each 3-byte ``[],`` is a 64-byte list), and each is about
# one item, a check or 50 read-back items (the largest item of that lab library: 13 KB).
MAX_SMALL_ANSWER_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
# Agent states the Edit tab badges (``capability().details["agent"]["state"]``).
AGENT_CONNECTED = "connected"
AGENT_UNREACHABLE = "unreachable"
AGENT_REJECTED = "rejected"
AGENT_INCOMPATIBLE = "incompatible"
# A node answers, but it doesn't have the Plex database role turned on.
AGENT_NO_ROLE = "no_role"
# The app refuses to send anything (``node_refusal``): the node is turned off, or the server's node record is gone.
AGENT_DISABLED = "disabled"
AGENT_NO_NODE = "no_node"
# The node reports a new id this app can't tie to the server's Plex (``NodeRegistry.plex_db_refusal``).
AGENT_UNCONFIRMED_NODE = "unconfirmed_node"
# What "Can't reach the Plex database node" goes on to say, per feature (``AgentClient.post``).
MARKERS_WAIT_FOR_THE_NODE = "Markers wait here until it answers again; nothing is lost."
LOUDNESS_WAITS_FOR_THE_NODE = "Loudness waits until it answers again; nothing is lost."
CHAPTERS_WAIT_FOR_THE_NODE = "Chapter thumbnails are tried again once it answers."
LIBRARY_HEALTH_WAITS_FOR_THE_NODE = "Library Health counts these once it answers again."


class AgentError(PublishError):
    """The agent couldn't be used (unreachable, wrong key, wrong version) — not a failure of the database itself."""


class AgentUnreachableError(AgentError):
    """The node didn't answer at all (down, restarting, unplugged): a later attempt may well succeed.

    Its state stays ``AGENT_UNAVAILABLE``, which the Edit tab and Setup Health explain; a feature that waits for a
    database it can't reach (loudness) tells this one apart from a refusal by its type.
    """


class NodeRefusedError(AgentError):
    """This app sent nothing: the node is turned off, its record is gone, or its new id is held (``node_refusal``).

    The Nodes page or the registry's next check lifts it at any moment, so a check that ends in one is never reused for
    a job's next file (``details["node_refused"]``).
    """


class NodeFullError(DatabaseBusyError):
    """The Plex database node ran nothing: as many of its requests as it lets wait for Plex's database already do.

    It answers at once rather than tie up one more of its threads (``nodes.server.plex_db``), so its health check and
    a worker node's task polls keep being answered while Plex keeps its database locked. ``AgentClient.post`` asks
    again until the request's own wait is used up, then raises it like any busy database: the file is tried again.
    An app from before it reads the same answer as a plain busy database.
    """


# Dotted release numbers, then an optional pre-release (rc1), dev number (.dev3) and local part (+g1234abc): the
# PEP 440 shapes setuptools-scm writes for a release, a :dev image and a local build. re.ASCII: "²" is a digit to \d
# but not to int(); the 9-digit cap keeps int() under its digit limit.
_VERSION_RE = re.compile(
    r"(\d{1,9}(?:\.\d{1,9})*)((?:a|b|rc)\d{0,9})?(?:\.?dev(\d{1,9}))?(?:\+[a-z0-9.]{1,64})?", re.ASCII | re.IGNORECASE
)


def _version_key(version: str) -> tuple[tuple[int, ...], bool, int] | None:
    """A version's sort key, or None when it isn't one this app can read.

    A dev or pre-release build comes after every earlier release and before the release it leads to (5.1.0 <
    5.1.1.dev3 < 5.1.1); two dev builds of one release order by their dev number. The local part is ignored.
    """
    match = _VERSION_RE.fullmatch(str(version or "").strip())
    if match is None:
        return None
    release, pre, dev = match.groups()
    final = pre is None and dev is None
    return tuple(int(p) for p in release.split(".")), final, int(dev or 0)


def agent_too_old(version: str) -> bool:
    """Whether an agent's reported version is older than ``MIN_AGENT_VERSION``.

    Args:
        version: The version the agent reports.

    Returns:
        True only when both versions parse (``_version_key``) and the agent's is lower. A version this app can't read
        (including the ``""`` of an answer with no ``agent`` block at all) is not "too old": neither this check nor
        the protocol one -- which passes an empty ``protocols`` list (``_read``) -- refuses such an answer on its own.
        Something that isn't our agent is turned away by the ``result`` it doesn't send, as "answered N instead of a
        result".
    """
    return agent_older_than(version, MIN_AGENT_VERSION)


def agent_older_than(version: str, needed: str) -> bool:
    """Whether both versions parse (``_version_key``) and ``version`` is lower than ``needed``."""
    theirs, mine = _version_key(version), _version_key(needed)
    return bool(theirs and mine and theirs < mine)


# --------------------------------------------------------------------------- codec
# One place both sides encode and decode, so the app and the agent can never disagree about a field.


def marker_to_json(marker: Marker) -> dict:
    """Serialise one decided marker."""
    return {
        "type": marker.type.value,
        "start_ms": marker.start_ms,
        "end_ms": marker.end_ms,
        "decided_by": list(marker.decided_by),
        "locked": marker.locked,
    }


def marker_from_json(raw: Any) -> Marker:
    """Read one decided marker.

    Raises:
        ValueError: The value isn't a marker.
    """
    if not isinstance(raw, dict):
        raise ValueError("marker must be an object")
    try:
        mtype = MarkerType(raw["type"])
        start, end = _as_int(raw["start_ms"]), _as_int(raw["end_ms"])
        decided_by = tuple(str(x) for x in raw.get("decided_by") or ())
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a marker: {exc}") from exc
    return Marker(mtype, start, end, decided_by, bool(raw.get("locked")))


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{value!r} is not a whole number")
    return value


def _as_opt_int(value: Any) -> int | None:
    return None if value is None else _as_int(value)


def _lenient_int(value: Any) -> int | None:
    try:
        return _as_int(value)
    except ValueError:
        return None


def _as_opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{value!r} is not text")
    return value


def part_to_json(part: Part) -> dict:
    """Serialise one ``media_parts`` row."""
    return {
        "id": part.id,
        "media_item_id": part.media_item_id,
        "file": part.file,
        "extra_data": part.extra_data,
        "proxy_type": part.proxy_type,
        "updated_at": part.updated_at,
    }


def part_from_json(raw: Any) -> Part:
    """Read one ``media_parts`` row.

    Raises:
        ValueError: The value isn't a part.
    """
    if not isinstance(raw, dict):
        raise ValueError("part must be an object")
    try:
        return Part(
            _as_int(raw["id"]),
            _as_int(raw["media_item_id"]),
            str(raw["file"]),
            _as_opt_str(raw.get("extra_data")),
            _as_opt_int(raw.get("proxy_type")),
            # Absent from an app or agent older than the staleness check, or not a whole number: unknown, which the
            # check treats as fresh.
            _lenient_int(raw.get("updated_at")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a media part: {exc}") from exc


def _types_to_json(types: frozenset[MarkerType]) -> list[str]:
    return sorted(t.value for t in types)


def _types_from_json(raw: Any) -> frozenset[MarkerType]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise ValueError("marker types must be a list")
    return frozenset(MarkerType(str(t)) for t in raw)


def _limits_to_json(limits: FileLimits | None) -> dict | None:
    if limits is None:
        return None
    return {"duration_ms": limits.duration_ms}


def _limits_from_json(raw: Any) -> FileLimits | None:
    """Read a file's limits; None (an app older than this agent sent none) leaves Plex's markers unchecked."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("limits must be an object")
    return FileLimits(_as_int(raw.get("duration_ms")))


def _markers_from_json(raw: Any) -> list[Marker]:
    if not isinstance(raw, list):
        raise ValueError("markers must be a list")
    return [marker_from_json(m) for m in raw]


def write_request_to_json(request: WriteRequest) -> dict:
    """Serialise one item's write."""
    return {
        "rating_key": request.rating_key,
        "parts": [part_to_json(p) for p in request.parts],
        "wanted": [marker_to_json(m) for m in request.wanted],
        "prior": [marker_to_json(m) for m in request.prior],
        "duration_ms": request.duration_ms,
        "own_prior": [marker_to_json(m) for m in request.own_prior],
        "calling_part_ids": list(request.calling_part_ids),
        "kept_types": _types_to_json(request.kept_types),
        "keep_plex": request.keep_plex,
        "limits": _limits_to_json(request.limits),
        "siblings": [
            {
                "rating_key": s.rating_key,
                "prior": [marker_to_json(m) for m in s.prior],
                "kept_types": _types_to_json(s.kept_types),
            }
            for s in request.siblings
        ],
    }


def _keys_from_json(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, list):
        raise ValueError("siblings must be a list")
    return tuple(_as_int(key) for key in raw)


def _sibling_states_from_json(raw: Any) -> tuple[SiblingState, ...]:
    if not isinstance(raw, list):
        raise ValueError("siblings must be a list")
    return tuple(
        SiblingState(_as_int(s["rating_key"]), _markers_from_json(s["prior"]), _types_from_json(s.get("kept_types")))
        for s in raw
    )


def write_request_from_json(raw: Any) -> WriteRequest:
    """Read one item's write.

    Raises:
        ValueError: The body isn't a write request.
    """
    if not isinstance(raw, dict):
        raise ValueError("write must be an object")
    try:
        return WriteRequest(
            rating_key=_as_int(raw["rating_key"]),
            parts=[part_from_json(p) for p in raw["parts"]],
            wanted=_markers_from_json(raw["wanted"]),
            prior=_markers_from_json(raw["prior"]),
            duration_ms=_as_opt_int(raw.get("duration_ms")),
            own_prior=_markers_from_json(raw.get("own_prior") or []),
            calling_part_ids=tuple(_as_int(i) for i in raw.get("calling_part_ids") or ()),
            kept_types=_types_from_json(raw.get("kept_types")),
            keep_plex=bool(raw.get("keep_plex")),
            limits=_limits_from_json(raw.get("limits")),
            # An app older than this field has no records of them: the agent writes them as having none.
            siblings=_sibling_states_from_json(raw.get("siblings") or []),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a write request: {exc}") from exc


def write_result_to_json(result: WriteResult) -> dict:
    """Serialise what one write did."""
    return {
        "changed": result.changed,
        "ours": [marker_to_json(m) for m in result.ours],
        "kept_types": _types_to_json(result.kept_types),
        "replaced_own": _types_to_json(result.replaced_own),
        "replaced_stale": _types_to_json(result.replaced_stale),
        "siblings": [
            {
                "rating_key": s.rating_key,
                "changed": s.changed,
                "ours": [marker_to_json(m) for m in s.ours],
                "kept_types": _types_to_json(s.kept_types),
            }
            for s in result.siblings
        ],
    }


def write_result_from_json(raw: Any) -> WriteResult:
    """Read what one write did.

    Raises:
        ValueError: The answer isn't a write result.
    """
    if not isinstance(raw, dict):
        raise ValueError("write result must be an object")
    try:
        return WriteResult(
            bool(raw["changed"]),
            _markers_from_json(raw["ours"]),
            _types_from_json(raw.get("kept_types")),
            _types_from_json(raw.get("replaced_own")),
            _types_from_json(raw.get("replaced_stale")),
            # An agent older than this field writes no other item: nothing to record for them.
            tuple(
                SiblingResult(
                    _as_int(s["rating_key"]),
                    bool(s["changed"]),
                    _markers_from_json(s["ours"]),
                    _types_from_json(s.get("kept_types")),
                )
                for s in raw.get("siblings") or []
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a write result: {exc}") from exc


def item_read_to_json(item: ItemRead) -> dict:
    """Serialise one item's parts, and which types' markers were made for an earlier file (None: not read)."""
    stale = None if item.stale_types is None else _types_to_json(item.stale_types)
    siblings = None if item.siblings is None else list(item.siblings)
    return {
        "exists": item.exists,
        "parts": [part_to_json(p) for p in item.parts],
        "stale_types": stale,
        "siblings": siblings,
    }


def item_read_from_json(raw: Any) -> ItemRead:
    """Read one item's parts.

    Raises:
        ValueError: The answer isn't an item.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("parts"), list):
        raise ValueError("not an item")
    # An agent older than this field doesn't say: unknown, so the pipeline and "Keep Plex's" behave as before it.
    stale = None if raw.get("stale_types") is None else _types_from_json(raw["stale_types"])
    siblings = None if raw.get("siblings") is None else _keys_from_json(raw["siblings"])
    return ItemRead(bool(raw.get("exists")), [part_from_json(p) for p in raw["parts"]], stale, siblings)


def shown_ask_to_json(ask: ShownAsk) -> dict:
    """Serialise one item of a read-back."""
    return {
        "rating_key": ask.rating_key,
        "ours": [marker_to_json(m) for m in ask.ours],
        "kept_types": _types_to_json(ask.kept_types),
        "item_files": None if ask.item_files is None else list(ask.item_files),
    }


def shown_ask_from_json(raw: Any) -> ShownAsk:
    """Read one item of a read-back.

    Raises:
        ValueError: The value isn't a read-back item.
    """
    if not isinstance(raw, dict):
        raise ValueError("read-back item must be an object")
    try:
        files = raw.get("item_files")
        return ShownAsk(
            _as_int(raw["rating_key"]),
            _markers_from_json(raw["ours"]),
            _types_from_json(raw.get("kept_types")),
            None if files is None else tuple(str(f) for f in files),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a read-back item: {exc}") from exc


def shown_batch_to_json(batch: ShownBatch) -> dict:
    """Serialise a read-back's answers."""
    return {
        "answers": [
            {
                "shown": None if a.shown is None else a.shown.value,
                "version_files": list(a.version_files),
                "siblings": list(a.siblings),
            }
            for a in batch.answers
        ],
        "unreadable": batch.unreadable,
    }


def shown_batch_from_json(raw: Any) -> ShownBatch:
    """Read a read-back's answers.

    Raises:
        ValueError: The answer isn't a read-back batch.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("answers"), list):
        raise ValueError("not a read-back batch")
    answers = []
    for item in raw["answers"]:
        if not isinstance(item, dict):
            raise ValueError("read-back answer must be an object")
        shown = item.get("shown")
        answers.append(
            ShownAnswer(
                None if shown is None else Shown(str(shown)),
                tuple(str(f) for f in item.get("version_files") or ()),
                _keys_from_json(item.get("siblings") or []),
            )
        )
    return ShownBatch(answers, bool(raw.get("unreadable")))


def report_to_json(report: CapabilityReport) -> dict:
    """Serialise a capability report."""
    return {"state": report.state.value, "message": report.message, "details": report.details}


def report_from_json(raw: Any) -> CapabilityReport:
    """Read a capability report a node sent.

    Its message and the text in its details (the paths, filesystem and users this app words its own sentences from)
    go to the app's log, the job's file rows and Setup Health, so they are relayed like any text a node sends
    (``protocol.relayed_text``).

    Raises:
        ValueError: The answer isn't a capability report.
    """
    if not isinstance(raw, dict):
        raise ValueError("not a capability report")
    try:
        details = raw.get("details")
        return CapabilityReport(
            Capability(str(raw["state"])),
            relayed_text(str(raw.get("message") or "")),
            {key: _relayed_detail(value) for key, value in details.items()} if isinstance(details, dict) else {},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"not a capability report: {exc}") from exc


def _relayed_detail(value: Any) -> Any:
    """A report detail with its text relayed: a string, or each string in a list (the unwritable paths)."""
    if isinstance(value, str):
        return relayed_text(value)
    if isinstance(value, list):
        return [relayed_text(item) if isinstance(item, str) else item for item in value]
    return value


# Refusal kinds an answer names, per exception type; anything else is a plain ``PublishError``. An app older than a kind
# reads it as "publish": the same message and state, only not retried as busy.
_ERROR_KINDS: dict[type[PublishError], str] = {ItemNotFoundError: "item_not_found", DatabaseBusyError: "busy"}


def error_to_json(exc: PublishError) -> dict:
    """Serialise a refusal so the app can raise the very same exception."""
    from ...loudness.plex_db import NotAnalysedError, SourceChangedError
    from .plex_chapters import ChapterError

    if isinstance(exc, SourceChangedError):
        # Loudness retries a changed source later instead of failing it, so the type has to survive the trip.
        return {"kind": "source_changed", "message": str(exc), "state": exc.state.value if exc.state else None}
    if isinstance(exc, NotAnalysedError):
        # Loudness skips an item Plex lists as an extra instead of failing it: its type has to survive the trip too.
        return {
            "kind": "not_analysed",
            "plex_type": exc.plex_type,
            "message": str(exc),
            "state": exc.state.value if exc.state else None,
        }
    if isinstance(exc, ChapterError):
        return {
            "kind": "chapter",
            "code": exc.code,
            "message": str(exc),
            "state": exc.state.value if exc.state else None,
        }
    answer: dict[str, Any] = {
        "kind": next((name for cls, name in _ERROR_KINDS.items() if isinstance(exc, cls)), "publish"),
        "message": str(exc),
        "state": exc.state.value if exc.state else None,
    }
    if isinstance(exc, NodeFullError):
        answer["node_full"] = True
    return answer


def error_from_json(raw: Any) -> PublishError:
    """Rebuild the refusal the agent raised, so a remote failure reads exactly like a local one.

    Args:
        raw: The ``error`` object of a refused answer.

    Returns:
        The same exception type, message and capability state the agent's ``LocalPlexDb`` raised.
    """
    if not isinstance(raw, dict):
        return PublishError("The Plex database node refused this write without saying why")
    message = relayed_text(str(raw.get("message") or "The Plex database node refused this write"))
    state = None
    with_state = raw.get("state")
    if with_state:
        try:
            state = Capability(str(with_state))
        except ValueError:
            state = None
    if raw.get("kind") == "chapter":
        from .plex_chapters import ChapterError

        return ChapterError(message, code=str(raw.get("code") or "registration"), state=state)
    if raw.get("kind") == "source_changed":
        from ...loudness.plex_db import SourceChangedError

        return SourceChangedError(message, state=state)
    plex_type = raw.get("plex_type")
    if raw.get("kind") == "not_analysed" and type(plex_type) is int:
        from ...loudness.plex_db import NotAnalysedError

        return NotAnalysedError(message, plex_type=plex_type, state=state)
    if raw.get("kind") == "busy" and raw.get("node_full") is True:
        return NodeFullError(message, state=state)
    kind = next((cls for cls, name in _ERROR_KINDS.items() if raw.get("kind") == name), PublishError)
    return kind(message, state=state)


def _time_left(body: dict) -> float:
    """How long a request's body lets the node wait for the database (its longest wait field), or 0."""
    return max((float(body[name]) for name in _WAIT_FIELDS if type(body.get(name)) in (int, float)), default=0.0)


def _with_time_left(body: dict, left: float) -> dict:
    """``body`` asked again with ``left`` seconds of wait at most."""
    return {**body, **{name: min(body[name], left) for name in _WAIT_FIELDS if type(body.get(name)) in (int, float)}}


# --------------------------------------------------------------------------- transport


class _TooLargeError(Exception):
    """An answer past what this app reads of it."""


_JSON = json.JSONDecoder()
_SPACES = " \t\n\r"
_NOT_SPACE = re.compile(f"[^{_SPACES}]")
# What a row's values may be: JSON's text, numbers, true/false and null.
_PLAIN_TYPES = frozenset({str, int, float, bool, type(None)})


class _RowsReader:
    """Library Health's answer (``{"ok", "agent", "result": {"rows": {name: [row, ...]}, "capability"}}``), parsed
    as it arrives: one row at a time, never the whole answer at once.

    Read whole and then parsed, the answer was in memory twice over, and JSON of tiny rows (``[],``) parses into ~25x
    its size: one answer under MAX_ANSWER_BYTES could take ~12 GB. Each row here must be a list of plain values
    (text, numbers, true/false, null) and counts what it takes in memory against MAX_ROWS_MEMORY_BYTES; everything
    outside the rows counts its characters against MAX_SMALL_ANSWER_BYTES.

    Args:
        pieces: The answer's bytes as they arrive.
        limit: The most bytes of the answer read.
    """

    def __init__(self, pieces: Iterator[bytes], limit: int) -> None:
        self._pieces = pieces
        self._limit = limit
        self._decode = codecs.getincrementaldecoder("utf-8")().decode
        self._text = ""
        self._at = 0
        self._ended = False
        self._read_bytes = 0
        self._rows_memory = 0
        self._rest = 0

    def read(self) -> dict:
        """The whole answer.

        Raises:
            ValueError: It isn't JSON, or not an object.
            RecursionError: JSON nested deeper than Python's parser goes.
            _TooLargeError: It is past one of the limits.
        """
        if not self._take("{"):
            raise ValueError("not a JSON object")
        answer = self._members(lambda key: self._result() if key == "result" else self._small())
        if self._next_char():
            raise ValueError("more after the answer")
        return answer

    def _result(self) -> Any:
        if not self._take("{"):
            return self._small()
        return self._members(lambda key: self._rows() if key == "rows" else self._small())

    def _rows(self) -> Any:
        if not self._take("{"):
            return self._small()
        return self._members(lambda _name: self._row_list())

    def _row_list(self) -> Any:
        if not self._take("["):
            return self._small()
        rows: list[list] = []
        if self._take("]"):
            return rows
        while True:
            row, _ = self._value(MAX_SMALL_ANSWER_BYTES)
            if type(row) is not list or not _PLAIN_TYPES.issuperset(map(type, row)):
                raise ValueError("a row isn't a list of values")
            # The row, its place in the list, and each value (small numbers and None are shared, but counted anyway).
            self._rows_memory += sys.getsizeof(row) + 8 + sum(map(sys.getsizeof, row))
            if self._rows_memory > MAX_ROWS_MEMORY_BYTES:
                raise _TooLargeError
            rows.append(row)
            if self._take("]"):
                return rows
            if not self._take(","):
                raise ValueError("rows not separated by commas")

    def _members(self, value_of: Callable[[str], Any]) -> dict:
        """An object's members (its ``{`` already read), each value read by ``value_of(key)``."""
        members: dict = {}
        if self._take("}"):
            return members
        while True:
            key = self._small()
            if not isinstance(key, str) or not self._take(":"):
                raise ValueError("not a JSON object")
            members[key] = value_of(key)
            if self._take("}"):
                return members
            if not self._take(","):
                raise ValueError("members not separated by commas")

    def _small(self) -> Any:
        """A value outside the rows."""
        value, length = self._value(MAX_SMALL_ANSWER_BYTES - self._rest)
        self._rest += length
        return value

    def _value(self, most: int) -> tuple[Any, int]:
        """The next JSON value, at most ``most`` characters long, and its length."""
        self._next_char()
        while True:
            try:
                value, end = _JSON.raw_decode(self._text, self._at)
            except ValueError:
                if self._ended:
                    raise
            else:
                # One that reaches the end of what has arrived may go on (a number): it is read again with more.
                if end - self._at > most:
                    raise _TooLargeError
                if end < len(self._text) or self._ended:
                    length, self._at = end - self._at, end
                    return value, length
            if len(self._text) - self._at > most:
                raise _TooLargeError
            self._more()

    def _take(self, char: str) -> bool:
        """Read ``char`` when it is the next character that isn't whitespace."""
        if self._next_char() != char:
            return False
        self._at += 1
        return True

    def _next_char(self) -> str:
        """The next character that isn't whitespace, left unread ("" at the end of the answer)."""
        while True:
            if self._at < len(self._text) and self._text[self._at] not in _SPACES:
                return self._text[self._at]
            found = _NOT_SPACE.search(self._text, self._at)
            if found:
                self._at = found.start()
                return self._text[self._at]
            self._at = len(self._text)
            if not self._more():
                return ""

    def _more(self) -> bool:
        """Add the next piece to the text read so far (dropping what was parsed); False at the end of the answer."""
        piece = b"" if self._ended else next(self._pieces, b"")
        self._read_bytes += len(piece)
        if self._read_bytes > self._limit:
            raise _TooLargeError
        self._ended = not piece
        self._text = self._text[self._at :] + self._decode(piece, final=self._ended)
        self._at = 0
        return bool(piece)


@dataclass(frozen=True)
class NodeRefusal:
    """Why this app sends nothing to a server's Plex database node: the message, the capability and the badge state."""

    message: str
    state: Capability
    agent_state: str
    # The node's id is still to be checked: the work waits, as for a node that doesn't answer, instead of failing.
    waits: bool = False


NO_NODE_REFUSAL = NodeRefusal(
    "This server has no Plex database node on the Nodes page. Choose one in Edit → Connection.",
    Capability.AGENT_UNAVAILABLE,
    AGENT_NO_NODE,
)
# Waits: the job's next run opens the database through the server's new choice.
REPOINTED_REFUSAL = NodeRefusal(
    "This server's Plex database node was changed in Edit → Connection after this job started, so this job sends "
    "nothing more through the old one. The next run uses the new choice.",
    Capability.AGENT_UNAVAILABLE,
    AGENT_UNREACHABLE,
    waits=True,
)


def node_in_use(settings: Any) -> str | None:
    """The id of the Plex database node these settings send the server's database work to, or None for none.

    Args:
        settings: That server's ``ServerMarkersSettings``.
    """
    if not getattr(settings, "agent_enabled", False):
        return None
    return str(getattr(settings, "agent_node_id", None) or "") or None


def saved_server_node_id(server_id: str) -> str | None:
    """The Plex database node a saved server uses now (``node_in_use``), or None when it uses none or is gone.

    Args:
        server_id: The server's id.
    """
    from ...web.settings_manager import get_settings_manager
    from ..settings import load_server

    for entry in get_settings_manager().get("media_servers") or []:
        if isinstance(entry, dict) and str(entry.get("id") or "") == server_id:
            return node_in_use(load_server(entry.get("markers"), str(entry.get("type") or "")))
    return None


def node_refusal(settings: Any, live_node_id: Callable[[], str | None] | None = None) -> NodeRefusal | None:
    """Why the server's Plex database node mustn't be used now, or None when it may.

    A node turned off on the Nodes page gets nothing, and neither does one that reports a new id this app can't tie
    to the server's Plex (a different machine given the same key and address would write the wrong Plex's database)
    until its key is typed again, nor one no check has heard give its id since this app started or since it last
    didn't answer (that work waits for the check). The node's record and the registry are read on every call, so a
    job that opened the database before the node was turned off is refused from its next request on.

    ``settings`` are the ones the database was opened with, so they name the node the server used then. A job also
    passes ``live_node_id``: a server re-pointed to another node, or to none, while the job runs gets a refusal that
    waits, instead of its work going on through the old node until the job ends.

    Args:
        settings: That server's ``ServerMarkersSettings``.
        live_node_id: The node the saved server uses now (``node_in_use``, ``saved_server_node_id``). None for a
            check of settings not saved yet (the Edit tab's), which has nothing to compare with.
    """
    if live_node_id is not None and live_node_id() != node_in_use(settings):
        return REPOINTED_REFUSAL
    if not getattr(settings, "agent_url", ""):
        return NO_NODE_REFUSAL
    name = getattr(settings, "agent_node_name", "")
    enabled = getattr(settings, "agent_node_enabled", True)
    node_id = getattr(settings, "agent_node_id", None)
    if node_id:
        from ...nodes.settings import node_record

        record = node_record(str(node_id))
        if record is None:
            return NO_NODE_REFUSAL
        name, enabled = str(record.get("name") or ""), bool(record.get("enabled", True))
    name = name or "This server's Plex database node"
    if not enabled:
        return NodeRefusal(
            f"{name} is turned off on the Nodes page, so this app doesn't read or write this Plex server's database "
            "through it. Turn it on there to continue.",
            Capability.AGENT_UNAVAILABLE,
            AGENT_DISABLED,
        )
    if node_id:
        from ...nodes.registry import get_registry

        hold = get_registry().plex_db_hold(str(node_id))
        if hold is not None and hold.waits:
            return NodeRefusal(hold.message, Capability.AGENT_UNAVAILABLE, AGENT_UNREACHABLE, waits=True)
        if hold is not None:
            return NodeRefusal(hold.message, Capability.AGENT_UNAVAILABLE, AGENT_UNCONFIRMED_NODE)
    return None


def saved_node_uid(settings: Any) -> str:
    """The id this app saved for the server's Plex database node, read now ("" before first contact, or for an old
    Plex helper, which has none). The node is the one ``settings`` name; ``node_refusal`` stops a job whose server was
    re-pointed since.

    Args:
        settings: That server's ``ServerMarkersSettings``.
    """
    node_id = getattr(settings, "agent_node_id", None)
    if not node_id:
        return ""
    from ...nodes.settings import node_record

    record = node_record(str(node_id))
    return str((record or {}).get("node_uid") or "")


def saved_node_address(settings: Any) -> tuple[str, str]:
    """The address and key saved for the server's Plex database node, read now. The node is the one ``settings`` name;
    ``node_refusal`` stops a job whose server was re-pointed since.

    Args:
        settings: That server's ``ServerMarkersSettings``.

    Returns:
        ``(url, key)`` from the node's record; the settings' own copy for a server with no node id.
    """
    node_id = getattr(settings, "agent_node_id", None)
    if not node_id:
        return str(getattr(settings, "agent_url", "") or ""), str(getattr(settings, "agent_token", "") or "")
    from ...nodes.settings import node_address

    url, key, _name = node_address(str(node_id))
    return url, key


def report_wrong_node(settings: Any) -> None:
    """Hold the server's Plex database node's work until a check hears which machine answers at its address now.

    Args:
        settings: That server's ``ServerMarkersSettings``.
    """
    node_id = getattr(settings, "agent_node_id", None)
    if node_id:
        from ...nodes.registry import get_registry

        get_registry().report_unavailable(str(node_id))


def _parsed(pieces: Iterator[bytes], limit: int, *, rows: bool) -> Any:
    """An answer's JSON, read from its ``pieces`` up to ``limit`` bytes; None when it isn't JSON.

    Raises:
        _TooLargeError: The answer is past ``limit`` (or, for ``rows``, one of ``_RowsReader``'s limits).
    """
    try:
        if rows:
            return _RowsReader(pieces, limit).read()
        body = bytearray()
        for chunk in pieces:
            body += chunk
            if len(body) > limit:
                raise _TooLargeError
        return json.loads(body)
    except (ValueError, RecursionError):  # RecursionError: JSON nested deeper than Python's parser goes
        return None


class AgentClient:
    """One agent's HTTP endpoint: the key, the protocol check and the error mapping, nothing about markers."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        name: str = "",
        session: Any = None,
        refusal: Callable[[], NodeRefusal | None] | None = None,
        node_uid: Callable[[], str] | None = None,
        wrong_node: Callable[[], None] | None = None,
        address: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        """Create the client.

        Args:
            url: The agent's base address, e.g. ``http://plex-host.lan:9494``.
            token: The key both sides share. Never logged, never in an error message.
            name: The node's name on the Nodes page, which messages call it by ("" names it by its address).
            session: A ``requests.Session`` to use (tests pass their own).
            refusal: Asked before every request why nothing may be sent to this node now (``node_refusal``); a
                refusal fails that request.
            node_uid: Asked before every request for the id this app saved for the node (``saved_node_uid``; ""
                before first contact). Each request names it, so another machine at the address runs nothing.
            wrong_node: Told when the node refused a request as another machine than that id
                (``report_wrong_node``).
            address: Asked before every request for the node's address and key (``saved_node_address``), which then
                replace ``url`` and ``token``: a node edited while a job runs gets that job's next request. A server
                re-pointed to another node is ``refusal``'s to stop.
        """
        self._set_address(url, token)
        self.name = str(name or "").strip()
        self._session = session or new_session()
        self._refusal_check = refusal
        self._node_uid = node_uid
        self._wrong_node = wrong_node
        self._address = address
        self.version = ""
        refused = refusal() if refusal is not None else None
        self.state = refused.agent_state if refused is not None else AGENT_UNREACHABLE
        # Plex's own machine identifier, as the agent reads it beside the database it would write.
        self.machine_identifier = ""
        self.capabilities: list[str] = []

    def _set_address(self, url: str, token: str) -> None:
        self.url = str(url or "").strip().rstrip("/")
        self._token = str(token or "")

    def describe(self, *, address: bool = False) -> str:
        """The node in a message the user reads, after "The Plex database node": its name, with its address too when
        ``address`` (where checking the address is the fix), or "at <address>" for a node without a name."""
        where = f"at {self.url or '(no address)'}"
        if not self.name:
            return where
        return f"{self.name} {where}" if address else self.name

    def post(
        self,
        path: str,
        body: dict,
        *,
        timeout: float,
        unreachable: str = MARKERS_WAIT_FOR_THE_NODE,
        limit: int = MAX_SMALL_ANSWER_BYTES,
        deadline_s: float | None = None,
        rows: bool = False,
    ) -> dict:
        """Send one request and return its ``result``.

        Args:
            path: The endpoint under the agent's base address, e.g. ``/v1/item/read``.
            body: The request body.
            timeout: Seconds allowed for the answer, on top of the connect timeout.
            unreachable: What happens to the feature's work while the node doesn't answer, for that message.
            limit: The most bytes of the answer read; a larger one is refused unparsed.
            deadline_s: Seconds the whole answer may take, from the request on (None: the connect timeout and
                ``timeout`` together, from when the headers arrive).
            rows: The answer is Library Health's rows (``_RowsReader``).

        Returns:
            The answer's ``result`` object.

        Raises:
            AgentUnreachableError: The agent didn't answer.
            AgentError: The agent refused the key, or speaks another protocol.
            NodeFullError: The node stayed full for as long as the body lets it wait (it is asked again until then).
            PublishError: The agent ran the request and it was refused (the original exception, rebuilt).
        """
        # A full node gives back the wait it would have spent on one of its threads: this app spends it instead,
        # asking again with what is left, and stops asking once the job is cancelled.
        give_up = time.monotonic() + _time_left(body)
        while True:
            try:
                return self._send(
                    self._session.post,
                    path,
                    {"json": body},
                    timeout=timeout,
                    unreachable=unreachable,
                    limit=limit,
                    deadline_s=deadline_s,
                    rows=rows,
                )
            except NodeFullError:
                if give_up - time.monotonic() <= NODE_FULL_RETRY_S or wait_cancelled():
                    raise
                time.sleep(NODE_FULL_RETRY_S)
            left = max(0.0, give_up - time.monotonic())
            body = _with_time_left(body, left)
            timeout = min(timeout, left + ANSWER_GRACE_S)

    def ping(self, *, timeout: float) -> dict:
        """Read advertised optional capabilities before sending their typed operations."""
        return self._send(self._session.get, "/v1/ping", {}, timeout=timeout)

    def _send(
        self,
        method: Callable[..., Any],
        path: str,
        extra: dict,
        *,
        timeout: float,
        unreachable: str = "",
        limit: int = MAX_SMALL_ANSWER_BYTES,
        deadline_s: float | None = None,
        rows: bool = False,
    ) -> dict:
        """Send one request and read its answer: never a redirect followed, never more than ``limit`` bytes, never
        past its deadline (``post``)."""
        self._refuse_early()
        uid = self._node_uid() if self._node_uid is not None else ""
        if self._address is not None:
            # Read after the id: an edit of the address clears the id, so the other order could send a request with
            # no id to the old address.
            self._set_address(*self._address())
        sent = AGENT_PROTOCOL_NODE_UID if uid else AGENT_PROTOCOL
        headers = {"Authorization": f"Bearer {self._token}", PROTOCOL_HEADER: str(sent), "Accept-Encoding": "identity"}
        if uid:
            headers[NODE_UID_HEADER] = uid
        timeout = max(1.0, timeout)
        sent_at = time.monotonic()
        try:
            response = method(
                f"{self.url}{path}",
                headers=headers,
                timeout=(CONNECT_TIMEOUT_S, timeout),
                # A redirect would carry the key and the request to wherever the node pointed.
                allow_redirects=False,
                stream=True,
                **extra,
            )
            # An explicit deadline counts from the request; the default one from the headers, so a slow start doesn't
            # take the body's time.
            deadline = (
                sent_at + deadline_s if deadline_s is not None else time.monotonic() + CONNECT_TIMEOUT_S + timeout
            )
            try:
                payload = self._payload(response, deadline, limit, rows=rows)
            finally:
                response.close()
        except requests.RequestException as exc:
            self.state = AGENT_UNREACHABLE
            # The exception text can hold the URL but never the key (it is a header).
            raise AgentUnreachableError(
                f"Can't reach the Plex database node {self.describe(address=True)} ({type(exc).__name__}). "
                f"{unreachable}".strip(),
                state=Capability.AGENT_UNAVAILABLE,
            ) from exc
        if response.status_code == 429:
            # Too many wrong keys came from this app's address (maybe from something else sharing it): the node
            # didn't compare this key, so it is a wait, not a refused key.
            self.state = AGENT_UNREACHABLE
            raise AgentUnreachableError(
                f"The Plex database node {self.describe()} asks this app to wait: too many wrong keys came from "
                f"this app's address. {unreachable}".strip(),
                state=Capability.AGENT_UNAVAILABLE,
            )
        return self._read(response.status_code, payload, sent=sent, unreachable=unreachable)

    def _payload(
        self, response: Any, deadline: float, limit: int = MAX_SMALL_ANSWER_BYTES, *, rows: bool = False
    ) -> Any:
        """The answer's JSON body, read up to ``limit`` bytes until ``deadline``; None when it isn't JSON.

        Raises:
            AgentError: A redirect, a compressed body, or a body larger than any answer.
            requests.RequestException: The answer didn't arrive whole by ``deadline``, or the connection broke.
        """
        if 300 <= response.status_code < 400:
            self.state = AGENT_UNREACHABLE
            raise AgentError(
                f"The Plex database node {self.describe(address=True)} answered with a redirect; this app doesn't "
                "follow one. Check the node's address.",
                state=Capability.AGENT_UNAVAILABLE,
            )
        encoding = str(response.headers.get("Content-Encoding") or "identity").strip().lower()
        payload = None
        too_large = False
        if encoding == "identity":
            try:
                payload = _parsed(answer_pieces(response, _READ_CHUNK_BYTES, deadline), limit, rows=rows)
            except _TooLargeError:
                too_large = True
        if encoding != "identity" or too_large:
            self.state = AGENT_UNREACHABLE
            raise AgentError(
                f"The Plex database node {self.describe()} sent an answer this app doesn't read "
                f"({'compressed' if encoding != 'identity' else 'too large'}).",
                state=Capability.AGENT_UNAVAILABLE,
            )
        return payload

    def refusal(self) -> NodeRefusal | None:
        """Why nothing may be sent to this node now, or None when requests may go out."""
        refusal = self._refusal_check() if self._refusal_check is not None else None
        if refusal is None and not self.url:
            refusal = NO_NODE_REFUSAL
        return refusal

    def _refuse_early(self) -> None:
        """Raise the node's current refusal (:meth:`refusal`) before sending; one that waits is raised as the node not
        answering, so each feature's work waits for it as it does for a node that is down."""
        refusal = self.refusal()
        if refusal is not None:
            self.state = refusal.agent_state
            error = AgentUnreachableError if refusal.waits else NodeRefusedError
            raise error(refusal.message, state=refusal.state)

    def require(self, capability: str, *, feature: str, needs: str, timeout: float = 5.0) -> None:
        """Make sure the helper advertises an optional capability before sending its requests.

        Every answer refreshes ``capabilities``, so this asks ``/v1/ping`` only when the last answer didn't list it.

        Args:
            capability: The capability name, e.g. ``loudness_v1``.
            feature: What needs it, for the message ("loudness analysis").
            needs: The helper version that first shipped it, for the message.
            timeout: Seconds allowed for the ping.

        Raises:
            AgentError: PLUGIN_OUTDATED when the helper doesn't have it (an older helper: nothing is sent), or the
                ping's own refusal.
        """
        if capability in self.capabilities:
            return
        self.ping(timeout=timeout)
        if capability not in self.capabilities:
            raise AgentError(
                f"The Plex database node {self.describe()} is version {self.version or 'unknown'}; {feature} "
                f"needs version {needs} or newer. Update the node.",
                state=Capability.PLUGIN_OUTDATED,
            )

    def _node_without_role(self) -> bool:
        """Whether a node answers at this address without the Plex database role (it 404s the role's routes).

        Its ``/v1/health`` carries a ``node`` block and no ``agent`` block; the role adds the agent block there.
        """
        deadline = time.monotonic() + 2 * CONNECT_TIMEOUT_S
        try:
            response = self._session.get(
                f"{self.url}/v1/health",
                headers={"Accept-Encoding": "identity"},
                timeout=(CONNECT_TIMEOUT_S, CONNECT_TIMEOUT_S),
                allow_redirects=False,
                stream=True,
            )
            try:
                payload = self._payload(response, deadline)
            finally:
                response.close()
        except (requests.RequestException, AgentError):
            return False
        return isinstance(payload, dict) and isinstance(payload.get("node"), dict) and "agent" not in payload

    def _read(self, status_code: int, payload: Any, *, sent: int = AGENT_PROTOCOL, unreachable: str = "") -> dict:
        if not isinstance(payload, dict):
            payload = {}
        advertised_agent = payload.get("agent")
        agent = advertised_agent if isinstance(advertised_agent, dict) else {}
        advertised = agent.get("capabilities")
        self.capabilities = advertised if isinstance(advertised, list) else []
        self.version = relayed_text(str(agent.get("version") or ""), AGENT_FIELD_MAX)
        advertised_protocols = agent.get("protocols")
        protocols = advertised_protocols if isinstance(advertised_protocols, list) else []
        if status_code == 401:
            self.state = AGENT_REJECTED
            raise AgentError(
                f"The Plex database node {self.describe()} refused this app's key. Set the same key on both sides.",
                state=Capability.AGENT_UNAVAILABLE,
            )
        error = payload.get("error")
        if status_code == 409 and isinstance(error, dict) and error.get("kind") == KIND_WRONG_NODE:
            # Another machine answers at the address with the node's key. It ran nothing, so the work waits, as for a
            # node that is down, while a check hears which machine it is.
            self.state = AGENT_UNREACHABLE
            logger.warning("The machine at {} isn't the Plex database node saved there; checking it again", self.url)
            if self._wrong_node is not None:
                self._wrong_node()
            raise AgentUnreachableError(
                f"The machine answering at {self.url or '(no address)'} isn't {f'{self.name}, ' if self.name else ''}"
                "the Plex database node saved there (it has another id), so it ran nothing. This app checks it "
                f"again. {unreachable}".strip(),
                state=Capability.AGENT_UNAVAILABLE,
            )
        speaks_ours = sent in protocols if protocols else True
        if not speaks_ours or (self.version and agent_too_old(self.version)):
            self.state = AGENT_INCOMPATIBLE
            named = self.version or "unknown"
            spoken = relayed_text(", ".join(str(p) for p in protocols), AGENT_FIELD_MAX) or "another protocol"
            # Which side has to move. An agent this app knows to be too old is updated, and so is one that speaks
            # only older protocols than the one sent (from before the node id check); anything else that doesn't
            # speak it has moved past us, and telling the user to update it again would send them in a circle --
            # including an agent that reports no version at all.
            if agent_too_old(self.version):
                advice = f"this app needs {MIN_AGENT_VERSION} or newer. Update the node."
            elif protocols and all(type(p) is int and p < sent for p in protocols):
                advice = f"it speaks protocol {spoken} and this app needs {sent}. Update the node."
            else:
                advice = f"it speaks protocol {spoken} and this app speaks {sent}. Update this app to match the node."
            raise AgentError(
                f"The Plex database node {self.describe()} is version {named}; {advice}",
                state=Capability.AGENT_UNAVAILABLE,
            )
        if status_code == 409 and isinstance(error, dict) and error.get("kind") == KIND_PROTOCOL:
            # The node speaks our protocol and still refused it (a proxy dropped the id header): every request ends
            # the same way, so this is the node's state, not one write's failure. Nothing ran.
            self.state = AGENT_INCOMPATIBLE
            raise AgentError(
                relayed_text(
                    str(
                        error.get("message") or f"The Plex database node {self.describe()} refused this app's protocol."
                    )
                ),
                state=Capability.AGENT_UNAVAILABLE,
            )
        if status_code == 404 and self._node_without_role():
            self.state = AGENT_NO_ROLE
            raise AgentError(
                f"The node {self.describe()} answers, but its Plex database role is off. Start it with that role "
                "(NODE_ROLES=worker,plex_db), or choose another node in Edit → Connection.",
                state=Capability.AGENT_UNAVAILABLE,
            )
        if status_code == 409 and isinstance(payload.get("error"), dict):
            self.state = AGENT_CONNECTED
            raise error_from_json(payload["error"])
        if status_code != 200 or not isinstance(payload.get("result"), dict):
            self.state = AGENT_UNREACHABLE
            raise AgentError(
                f"The Plex database node {self.describe()} answered {status_code} instead of a result.",
                state=Capability.AGENT_UNAVAILABLE,
            )
        self.state = AGENT_CONNECTED
        return payload["result"]

    def agent_details(self) -> dict:
        """What the Edit tab shows about the agent: address, version, connection state and which Plex it serves."""
        return {
            "url": self.url,
            "name": self.name,
            "version": self.version,
            "state": self.state,
            "machine_identifier": self.machine_identifier,
        }


class RemotePlexDb(PlexDatabase):
    """Plex's database as the Plex marker agent sees it, on Plex's own machine.

    Every method is the one ``LocalPlexDb`` runs there; this class only carries the arguments and the answer, and
    turns a transport failure into the ``AGENT_UNAVAILABLE`` capability the Edit tab explains.
    """

    # A write can fail with its answer lost after the agent's transaction committed (a blip on the way back), so
    # what is ours after a failure is unknown — unlike the in-process writer, where one transaction settles it.
    atomic_writes = False

    def __init__(
        self,
        url: str,
        token: str,
        *,
        name: str = "",
        session: Any = None,
        refusal: Callable[[], NodeRefusal | None] | None = None,
        node_uid: Callable[[], str] | None = None,
        wrong_node: Callable[[], None] | None = None,
        address: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        """Create the transport.

        Args:
            url: The agent's base address.
            token: The key both sides share.
            name: The node's name, for messages (``AgentClient``).
            session: A ``requests.Session`` (tests pass their own).
            refusal: Asked before every call why nothing may be sent to the node now (``node_refusal``).
            node_uid: The id this app saved for the node, named on every call (``AgentClient``).
            wrong_node: Told when the node refused a call as another machine than that id.
            address: The node's address and key as saved now, read before every call (``AgentClient``).
        """
        self._client = AgentClient(
            url,
            token,
            name=name,
            session=session,
            refusal=refusal,
            node_uid=node_uid,
            wrong_node=wrong_node,
            address=address,
        )

    def agent_details(self) -> dict:
        """The agent block for ``capability().details``."""
        return self._client.agent_details()

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _with_agent(self, report: CapabilityReport) -> CapabilityReport:
        return CapabilityReport(report.state, report.message, {**report.details, "agent": self.agent_details()})

    def _failed_check(self, exc: PublishError) -> CapabilityReport:
        """The report of a check the node couldn't run.

        A node that stayed full is a busy database (``db_busy``), and one that didn't answer, or that is held until a
        check hears its id (``node_refusal``), is work that waits for it (``node_waits``). One this app refuses
        (``NodeRefusedError``: turned off, or a new id held until its key is typed again) is ``node_refused``. A job
        reuses none of them for its next files: a check, or the user on the Nodes page, may lift it seconds later.
        """
        report = report_of_failure(exc, Capability.AGENT_UNAVAILABLE)
        if isinstance(exc, AgentUnreachableError):
            report = CapabilityReport(report.state, report.message, {**report.details, "node_waits": True})
        elif isinstance(exc, NodeRefusedError):
            report = CapabilityReport(report.state, report.message, {**report.details, "node_refused": True})
        return self._with_agent(report)

    def file_checks(self, *, deadline: float) -> CapabilityReport:
        """Ask the agent to check the database file on its own machine.

        Args:
            deadline: When to stop waiting.

        Returns:
            The agent's own report (its path, its filesystem, its lock proof), with the agent's state in
            ``details["agent"]``; AGENT_UNAVAILABLE when the agent itself couldn't be used.
        """
        remaining = self._remaining(deadline)
        try:
            result = self._client.post("/v1/checks/file", {"deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S)
            self._client.machine_identifier = str(result.get("machine_identifier") or "")
            return self._with_agent(report_from_json(result.get("report")))
        except PublishError as exc:
            return self._failed_check(exc)
        except ValueError as exc:
            logger.debug("Plex marker agent {}: unreadable answer: {}", self._client.describe(), exc)
            return self._with_agent(
                CapabilityReport(
                    Capability.AGENT_UNAVAILABLE,
                    f"The Plex database node {self._client.describe()} sent an answer this app can't read.",
                )
            )

    def db_checks(self, *, deadline: float) -> CapabilityReport:
        """Ask the agent to check the schema, the marker data and Plex's marker tag row.

        Args:
            deadline: When to stop waiting.

        Returns:
            The agent's report, or AGENT_UNAVAILABLE when the agent itself couldn't be used.
        """
        remaining = self._remaining(deadline)
        try:
            result = self._client.post("/v1/checks/db", {"deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S)
            return self._with_agent(report_from_json(result.get("report")))
        except PublishError as exc:
            return self._failed_check(exc)
        except ValueError as exc:
            logger.debug("Plex marker agent {}: unreadable answer: {}", self._client.describe(), exc)
            return self._with_agent(
                CapabilityReport(
                    Capability.AGENT_UNAVAILABLE,
                    f"The Plex database node {self._client.describe()} sent an answer this app can't read.",
                )
            )

    def read_item(self, rating_key: int, *, deadline: float) -> ItemRead:
        """Read one item's live parts through the agent.

        Args:
            rating_key: Plex's metadata item id.
            deadline: When to stop waiting.

        Returns:
            Whether the database knows the item, its live parts, and ``stale_types`` (None from an agent older than
            that field).

        Raises:
            PublishError: The agent refused, or couldn't be used.
        """
        remaining = self._remaining(deadline)
        result = self._client.post(
            "/v1/item/read", {"rating_key": rating_key, "deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S
        )
        try:
            return item_read_from_json(result.get("item"))
        except ValueError as exc:
            raise self._unreadable(exc) from exc

    def write_item(self, request: WriteRequest, *, deadline: float) -> WriteResult:
        """Run one item's write on the agent, in one transaction there.

        Args:
            request: The item, the parts its markers were decided for, and those markers.
            deadline: When to stop waiting.

        Returns:
            Whether the database changed, and what the item shows as ours.

        Raises:
            PublishError: Nothing was written (the agent's own refusal, or the agent couldn't be used).
        """
        remaining = self._remaining(deadline)
        body = {"deadline_s": remaining, "write": write_request_to_json(request)}
        result = self._client.post("/v1/item/write", body, timeout=remaining + ANSWER_GRACE_S)
        try:
            return write_result_from_json(result.get("write"))
        except ValueError as exc:
            raise self._unreadable(exc) from exc

    def shown_many(
        self, items: list[ShownAsk], *, timeout_s: float, cancel_check: Callable[[], bool] | None = None
    ) -> ShownBatch:
        """Read items back through the agent, in chunks so a cancel is still answered quickly.

        Args:
            items: What to ask about each item.
            timeout_s: The longest one item's read waits for the database locks, on the agent.
            cancel_check: True once the job is cancelled; checked between chunks.

        Returns:
            One answer per item read, and whether the database stopped being readable at all. A chunk the agent
            couldn't be asked for at all ends the call with ``unreadable``.
        """
        answers: list[ShownAnswer] = []
        for start in range(0, len(items), READ_BACK_CHUNK):
            if cancel_check and cancel_check():
                return ShownBatch(answers, False)
            chunk = items[start : start + READ_BACK_CHUNK]
            body = {
                "timeout_s": timeout_s,
                "budget_s": READ_BACK_BUDGET_S,
                "items": [shown_ask_to_json(a) for a in chunk],
            }
            try:
                result = self._client.post("/v1/items/shown", body, timeout=READ_BACK_BUDGET_S + ANSWER_GRACE_S)
                batch = shown_batch_from_json(result.get("batch"))
            except (PublishError, ValueError) as exc:
                logger.debug("Plex marker agent {}: read-back stopped: {}", self._client.describe(), exc)
                return ShownBatch(answers, True)
            answers.extend(batch.answers)
            # The agent stopped early: either the database went unreadable (its own flag) or its budget ran out,
            # which leaves the items unread exactly as a cancel does.
            if batch.unreadable:
                return ShownBatch(answers, True)
            if len(batch.answers) < len(chunk):
                return ShownBatch(answers, False)
        return ShownBatch(answers, False)

    def item_exists(self, rating_key: int, *, deadline: float) -> bool:
        """Whether the agent's database has this rating key.

        Args:
            rating_key: Plex's metadata item id.
            deadline: When to stop waiting.

        Returns:
            True when the item is there.

        Raises:
            PublishError: The agent refused, or couldn't be used.
        """
        remaining = self._remaining(deadline)
        result = self._client.post(
            "/v1/item/exists", {"rating_key": rating_key, "deadline_s": remaining}, timeout=remaining + ANSWER_GRACE_S
        )
        if not isinstance(result.get("exists"), bool):
            raise self._unreadable(ValueError("no exists flag"))
        return result["exists"]

    def node_machine_identifier(self, *, timeout: float = 5.0) -> str:
        """The machine identifier of the Plex the node is beside, as it reads it now ("" when it can't say).

        Raises:
            PublishError: The node couldn't be asked.
        """
        answer = self._client.ping(timeout=timeout).get("machine_identifier")
        self._client.machine_identifier = answer if isinstance(answer, str) else ""
        return self._client.machine_identifier

    def loudness(self, operation: str, body: dict, *, deadline: float, needs: str) -> dict:
        """Run one fixed loudness operation (``/v1/loudness/{operation}``) on the helper.

        Args:
            operation: ``file_checks``, ``check``, ``streams``, ``snapshot``, ``write`` or ``mark``.
            body: The operation's fields (``loudness.guard.RemoteLoudnessDb`` builds them).
            deadline: When to stop waiting.
            needs: The helper version that first ran loudness, for the "update the helper" message.

        Returns:
            The answer's ``result``.

        Raises:
            PublishError: The helper refused (the original exception, rebuilt), is too old for loudness, or couldn't
                be used.
        """
        from ...loudness.plex_db import LOUDNESS_CAPABILITY

        self._client.require(LOUDNESS_CAPABILITY, feature="loudness analysis", needs=needs)
        remaining = self._remaining(deadline)
        result = self._client.post(
            f"/v1/loudness/{operation}",
            {**body, "deadline_s": remaining},
            timeout=remaining + ANSWER_GRACE_S,
            unreachable=LOUDNESS_WAITS_FOR_THE_NODE,
        )
        if result.get("capability") != LOUDNESS_CAPABILITY:
            raise self._unreadable(ValueError("no loudness capability in the answer"))
        return result

    def require_library_health(self, *, needs: str) -> None:
        """Raise the "update the helper" refusal unless the helper can run the Library Health queries."""
        from ...library_health.plex_rows import LIBRARY_HEALTH_CAPABILITY

        self._client.require(LIBRARY_HEALTH_CAPABILITY, feature="Library Health's database counts", needs=needs)

    def library_health_rows(
        self,
        section_ids: list[int],
        *,
        want_markers: bool,
        want_chapters: bool,
        deadline: float,
        needs: str,
        machine_identifier: str = "",
    ) -> dict:
        """The Library Health queries' rows (``library_health.plex_rows.read_rows``), run on the helper.

        Args:
            section_ids: Plex library section ids to count.
            want_markers: Also read the intro/credits marker rows.
            want_chapters: Also read chapters and durations.
            deadline: When to stop waiting.
            needs: The helper version that first ran these queries, for the "update the helper" message.
            machine_identifier: The server's Plex ``machineIdentifier``; the node refuses when it is beside another
                Plex ("" = unknown, allowed).

        Returns:
            The rows, in ``read_rows``' shape.

        Raises:
            PublishError: The helper refused (the original exception, rebuilt, also for a read that failed partway),
                is too old for Library Health counts, or couldn't be used.
        """
        from ...library_health.plex_rows import LIBRARY_HEALTH_CAPABILITY

        self.require_library_health(needs=needs)
        remaining = self._remaining(deadline)
        body = {
            "section_ids": list(section_ids),
            "want_markers": want_markers,
            "want_chapters": want_chapters,
            "machine_identifier": machine_identifier,
            "deadline_s": remaining,
        }
        result = self._client.post(
            "/v1/library-health/rows",
            body,
            timeout=remaining + ANSWER_GRACE_S,
            unreachable=LIBRARY_HEALTH_WAITS_FOR_THE_NODE,
            limit=MAX_ANSWER_BYTES,
            deadline_s=remaining + ROWS_ANSWER_S,
            rows=True,
        )
        if isinstance(result.get("error"), dict):
            # The node's read failed after its answer's status went out: the refusal comes in the answer instead.
            raise error_from_json(result["error"])
        if result.get("capability") != LIBRARY_HEALTH_CAPABILITY or not isinstance(result.get("rows"), dict):
            raise self._unreadable(ValueError("no Library Health rows in the answer"))
        return result["rows"]

    def _unreadable(self, exc: Exception) -> AgentError:
        logger.debug("Plex marker agent {}: unreadable answer: {}", self._client.describe(), exc)
        return AgentError(
            f"The Plex database node {self._client.describe()} sent an answer this app can't read.",
            state=Capability.AGENT_UNAVAILABLE,
        )


def plex_database(
    settings: Any,
    *,
    path_provider: Callable[[], str | None],
    label: str,
    mountinfo_path: str = "/proc/self/mountinfo",
    session: Any = None,
    live_node_id: Callable[[], str | None] | None = None,
) -> LocalPlexDb | RemotePlexDb:
    """The database half for one Plex server: the agent when one is set up, the local file otherwise.

    Args:
        settings: That server's ``ServerMarkersSettings``.
        path_provider: Returns the database path this app can see (used only without an agent).
        label: The server's name, for log lines.
        mountinfo_path: For tests.
        session: A ``requests.Session`` for the agent (tests pass their own).
        live_node_id: A job's: the node the saved server uses now, so a server re-pointed while the job runs sends
            nothing more through the old node (``node_refusal``).

    Returns:
        A ``RemotePlexDb`` when the server has a Plex database node switched on, else a ``LocalPlexDb``. A node that
        mustn't be used (``node_refusal``: there is none, its record is gone, or it is turned off) still gets a
        ``RemotePlexDb``, which refuses every call: falling back to this app's own copy of the
        folder would write a database Plex isn't reading.
    """
    if getattr(settings, "agent_enabled", False):
        return RemotePlexDb(
            settings.agent_url,
            getattr(settings, "agent_token", ""),
            name=getattr(settings, "agent_node_name", ""),
            session=session,
            refusal=lambda: node_refusal(settings, live_node_id),
            node_uid=lambda: saved_node_uid(settings),
            wrong_node=lambda: report_wrong_node(settings),
            address=lambda: saved_node_address(settings),
        )
    return LocalPlexDb(path_provider, label=label, mountinfo_path=mountinfo_path)
