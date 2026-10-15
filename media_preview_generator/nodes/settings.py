"""The ``nodes`` list in settings.json: validation, key masking and lookups (spec §4).

A node record is ``{"id", "node_uid", "name", "url", "key", "enabled", "path_mappings", "added_at"}``. ``id`` is
assigned here and never changes (worker group members and Plex servers point at it); ``node_uid`` is the id the node
itself reports, recorded on first contact. The key is a secret: it leaves the app only in the node's Authorization
header and comes back from the API as ``****``.
"""

from __future__ import annotations

import copy
import urllib.parse
import uuid
from datetime import UTC, datetime
from typing import Any

from .protocol import PathMapping, sendable_key

KEY_MASK = "****"
NAME_MAX_LEN = 80
URL_MAX_LEN = 500
KEY_MAX_LEN = 200
MAX_MAPPINGS = 50


def new_node_id() -> str:
    """A fresh app-side node id."""
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def normalise_url(raw: object) -> tuple[str | None, str]:
    """Validate a node address.

    Args:
        raw: The posted address.

    Returns:
        ``(url, "")`` without a trailing slash, or ``(None, message)``.
    """
    url = str(raw or "").strip().rstrip("/")
    if not url:
        return None, "Enter the node's address, e.g. http://nuc.lan:9494"
    shape_err = "The node's address must be an http(s) address, e.g. http://nuc.lan:9494"
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port  # Raises ValueError for a port that isn't a number from 0 to 65535.
    except ValueError:
        return None, shape_err
    if parsed.scheme not in ("http", "https") or not parsed.hostname or port == 0 or any(c.isspace() for c in url):
        return None, shape_err
    # A bare "?" or "#" parses as an empty query or fragment, and would swallow every route path sent after it.
    if "@" in parsed.netloc or "?" in url or "#" in url or len(url) > URL_MAX_LEN:
        return None, "The node's address must be a plain address with no username, password or query"
    return url, ""


def normalise_key(raw: object, stored: str) -> tuple[str | None, str]:
    """Validate a node key; ``****`` (or leaving it out) keeps the stored one.

    Args:
        raw: The posted key, or None when the field was left out.
        stored: The key on record ("" for a new node).

    Returns:
        ``(key, "")`` or ``(None, message)``.
    """
    key = stored if raw is None or raw == KEY_MASK else str(raw).strip()
    if not key:
        return None, "Enter the node's key (shown in the node's log the first time it starts)"
    if key != stored and not (len(key) <= KEY_MAX_LEN and sendable_key(key)):
        return None, f"The node's key must be up to {KEY_MAX_LEN} printable ASCII characters with no spaces"
    return key, ""


def normalise_mappings(raw: object) -> tuple[list[dict[str, str]] | None, str]:
    """Validate a node's path mapping rows.

    ``app_prefix`` is the folder as this app sees it (always POSIX, the app runs in Linux Docker); ``node_prefix``
    is the same folder as the node sees it, which may be a Windows path such as ``D:\\Media``.

    Args:
        raw: The posted ``path_mappings`` list (None means none).

    Returns:
        ``(rows, "")`` or ``(None, message)``.
    """
    if raw is None:
        return [], ""
    if not isinstance(raw, list):
        return None, "path_mappings must be a list"
    if len(raw) > MAX_MAPPINGS:
        return None, f"Use at most {MAX_MAPPINGS} folder mappings"
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "Each folder mapping must be an object with app_prefix and node_prefix"
        app_prefix = str(entry.get("app_prefix") or "").strip()
        node_prefix = str(entry.get("node_prefix") or "").strip()
        if not app_prefix.startswith("/"):
            return None, "Each folder mapping's app folder must start with / (the folder as this app sees it)"
        if not node_prefix:
            return None, f"Enter the folder the node sees for {app_prefix}"
        key = app_prefix.rstrip("/") or "/"
        if key in seen:
            return None, f"{app_prefix} is mapped twice; keep one row per app folder"
        seen.add(key)
        rows.append({"app_prefix": app_prefix, "node_prefix": node_prefix})
    return rows, ""


def validate_node(raw: object, *, existing: dict | None, others: list[dict]) -> tuple[dict | None, str]:
    """Validate one posted node against the record it updates and the other nodes.

    Fields left out keep their stored value. ``id``, ``node_uid`` and ``added_at`` are never taken from a client
    for an existing node; a new node gets a fresh id. A new address clears ``node_uid`` (it is recorded again on
    first contact), so re-pointing a node at another machine isn't refused as "a different node".

    Args:
        raw: The posted fields.
        existing: The stored record being edited, or None for a new node.
        others: Every other stored node (for the unique-name rule).

    Returns:
        ``(record, "")`` or ``(None, message)``.
    """
    if not isinstance(raw, dict):
        return None, "A node must be an object"
    base = existing or {}
    name = str(raw.get("name", base.get("name", "")) or "").strip()
    if not name:
        return None, "Give the node a name"
    if len(name) > NAME_MAX_LEN:
        return None, f"The node's name must be at most {NAME_MAX_LEN} characters"
    if any(str(o.get("name") or "").strip().casefold() == name.casefold() for o in others):
        return None, f"Another node is already called {name}"
    url, err = normalise_url(raw.get("url", base.get("url")))
    if url is None:
        return None, err
    key, err = normalise_key(raw.get("key"), str(base.get("key") or ""))
    if key is None:
        return None, err
    mappings, err = normalise_mappings(raw.get("path_mappings", base.get("path_mappings")))
    if mappings is None:
        return None, err
    enabled = raw.get("enabled", base.get("enabled", True))
    if type(enabled) is not bool:
        return None, "enabled must be true or false"
    uid = base.get("node_uid") if existing is not None else raw.get("node_uid")
    if existing is not None and url != normalise_url(base.get("url"))[0]:
        # A new address may be a different machine: the id is recorded again on its first answer.
        uid = None
    return {
        "id": str(base.get("id") or "") or new_node_id(),
        "node_uid": str(uid) if uid else None,
        "name": name,
        "url": url,
        "key": key,
        "enabled": enabled,
        "path_mappings": mappings,
        "added_at": str(base.get("added_at") or "") or _now_iso(),
    }, ""


def validate_nodes(raw: object) -> tuple[list[dict] | None, str]:
    """Validate a whole ``nodes`` list as stored (each record keeps its id, uid and key).

    Args:
        raw: The list to store.

    Returns:
        ``(nodes, "")`` or ``(None, message)``.
    """
    if not isinstance(raw, list):
        return None, "nodes must be a list"
    out: list[dict] = []
    seen_ids: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict) or not str(entry.get("id") or ""):
            return None, "Each node needs an id"
        node_id = str(entry["id"])
        if node_id in seen_ids:
            return None, f"Node id {node_id} is used twice"
        seen_ids.add(node_id)
        stored = {
            "id": node_id,
            "node_uid": entry.get("node_uid"),
            "url": entry.get("url"),
            "key": str(entry.get("key") or ""),
            "added_at": entry.get("added_at"),
        }
        record, err = validate_node({**entry, "key": None}, existing=stored, others=out)
        if record is None:
            return None, err
        out.append(record)
    return out, ""


def mask_node(node: dict) -> dict:
    """A copy of a node record safe to send to a browser: the key becomes ``****``."""
    out = copy.deepcopy(node)
    out["key"] = KEY_MASK if node.get("key") else ""
    return out


def find_node(nodes: object, node_id: object) -> dict | None:
    """The stored record with this id, or None."""
    if not isinstance(nodes, list) or not node_id:
        return None
    return next((n for n in nodes if isinstance(n, dict) and str(n.get("id") or "") == str(node_id)), None)


def node_mappings(node: dict) -> list[PathMapping]:
    """A record's mapping rows as :class:`PathMapping` (rows that don't parse are skipped)."""
    out: list[PathMapping] = []
    for row in node.get("path_mappings") or []:
        try:
            out.append(PathMapping.from_json(row))
        except ValueError:
            continue
    return out


def stored_nodes() -> list[dict]:
    """The ``nodes`` list from the settings manager (a deep copy)."""
    from ..web.settings_manager import get_settings_manager

    return get_settings_manager().nodes


def node_record(node_id: str | None) -> dict | None:
    """The stored record for a node id (a copy), or None when there is no such node.

    Args:
        node_id: The app-side node id.
    """
    if not node_id:
        return None
    from ..web.settings_manager import get_settings_manager

    node = find_node(get_settings_manager().get("nodes"), node_id)
    return copy.deepcopy(node) if node is not None else None


def node_address(node_id: str | None) -> tuple[str, str, str]:
    """The ``(url, key, name)`` on record for a node id; empty strings when there is no such node.

    The Plex database role reads its address from here, so the node record is the one place the address and key
    live.

    Args:
        node_id: The app-side node id.
    """
    node = node_record(node_id)
    if node is None:
        return "", "", ""
    return str(node.get("url") or ""), str(node.get("key") or ""), str(node.get("name") or "")


def default_record(**fields: Any) -> dict:
    """A record with every key present (for migrations that build one without a client)."""
    record = {
        "id": new_node_id(),
        "node_uid": None,
        "name": "",
        "url": "",
        "key": "",
        "enabled": True,
        "path_mappings": [],
        "added_at": _now_iso(),
    }
    record.update(fields)
    return record
