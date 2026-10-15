"""The ``nodes`` list in settings.json: validation, key masking and lookups (spec §4).

A node record is ``{"id", "node_uid", "name", "url", "key", "enabled", "path_mappings", "added_at"}``. ``id`` is
assigned here and never changes (worker group members and Plex servers point at it); ``node_uid`` is the id the node
itself reports, recorded on contact. A reinstalled worker node reports a new id, which replaces the old one: the
saved key at the saved address is what makes it this node; the id tells Add node that a node is already added. A
Plex database node's new id replaces the old one only when its Plex is the server's; otherwise typing its key again
(which clears the saved id) confirms it. The key
is a secret: it leaves the app only in the node's Authorization header and comes back from the API as ``****``.
"""

from __future__ import annotations

import copy
import ipaddress
import re
import unicodedata
import urllib.parse
import uuid
from datetime import UTC, datetime
from typing import Any

from .protocol import KEY_MAX_LEN, PathMapping, sendable_key

KEY_MASK = "****"
NAME_MAX_LEN = 80
URL_MAX_LEN = 500
MAX_MAPPINGS = 50
# "/v1", or a route under it, at the end of an address ("http://nuc:9494/v1/health").
_NODE_ROUTE_TAIL = re.compile(r"/v1(?:/[a-z/-]*)?$")
# One label of a host name as requests sends it (IDNA-encoded): letters, digits, hyphens and underscores (Docker
# service names have them), 1 to 63 of them.
_HOST_LABEL = re.compile(r"[A-Za-z0-9_-]{1,63}")


def new_node_id() -> str:
    """A fresh app-side node id."""
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sendable_host(url: str) -> bool:
    """Whether requests can send to ``url``: it prepares it, and its host is an IP address or a well-formed name.

    requests prepares ``nas..lan`` (an empty label) or a 64-character label, then urllib3 refuses it at connect time;
    each check of such a node would fail with a traceback instead of reading as a wrong address.
    """
    import requests
    import urllib3

    try:
        prepared = requests.PreparedRequest()
        prepared.prepare_url(url, None)
        host = urllib3.util.parse_url(prepared.url).host or ""
    except (requests.RequestException, urllib3.exceptions.HTTPError, ValueError, UnicodeError):
        return False
    if host.startswith("["):
        return True  # An IPv6 address, which urllib3 has just parsed.
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    name = host[:-1] if host.endswith(".") else host
    return 0 < len(name) <= 253 and all(_HOST_LABEL.fullmatch(label) for label in name.split("."))


def normalise_url(raw: object) -> tuple[str | None, str]:
    """Validate a node address.

    Args:
        raw: The posted address.

    Returns:
        ``(url, "")`` without a trailing slash, its scheme and host in lower case (``http://NUC:9494`` is the same
        address as ``http://nuc:9494``), or ``(None, message)``.
    """
    if raw is not None and not isinstance(raw, str):
        return None, "The node's address must be text"
    url = (raw or "").strip().rstrip("/")
    # Every node route starts /v1/, so an address ending in one is a route pasted from the docs or the node's log.
    url = _NODE_ROUTE_TAIL.sub("", url)
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
    if not _sendable_host(url):
        return None, "The node's address has a host name that can't be used, e.g. two dots in a row. Check it."
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "")), ""


def normalise_key(raw: object, stored: str) -> tuple[str | None, str]:
    """Validate a node key; ``****`` (or leaving it out) keeps the stored one.

    Args:
        raw: The posted key, or None when the field was left out.
        stored: The key on record ("" for a new node).

    Returns:
        ``(key, "")`` or ``(None, message)``.
    """
    if raw is not None and not isinstance(raw, str):
        return None, "The node's key must be text"
    key = stored if raw is None or raw == KEY_MASK else raw.strip()
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


def name_key(name: str) -> str:
    """A name as the unique-name rule compares it: names that look the same ("Café" typed two ways, "ＮＵＣ" and
    "nuc", "nuc  1" and "nuc 1", which a page shows alike) compare equal."""
    return " ".join(unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold()).split())


def _hidden(ch: str) -> bool:
    """Whether the name rule refuses ``ch``: a control or format character (a tab, a zero-width joiner, a
    right-to-left mark) or a line or paragraph break."""
    category = unicodedata.category(ch)
    return category[0] == "C" or category in ("Zl", "Zp")


def visible_name(text: str) -> str:
    """``text`` with what the name rule refuses taken out, for a name the app makes from other text (a Plex server's
    name): line breaks and tabs become spaces, hidden characters go, and runs of spaces become one."""
    return " ".join("".join(" " if ch.isspace() else ch for ch in text if ch.isspace() or not _hidden(ch)).split())


def validate_node(raw: object, *, existing: dict | None, others: list[dict]) -> tuple[dict | None, str]:
    """Validate one posted node against the record it updates and the other nodes.

    Fields left out keep their stored value. ``id``, ``node_uid`` and ``added_at`` are never taken from a client
    for an existing node; a new node gets a fresh id. A new address, or the key typed again, clears ``node_uid``
    (it is recorded again on the next answer).

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
    name = raw.get("name", base.get("name", ""))
    if name is not None and not isinstance(name, str):
        return None, "The node's name must be text"
    name = (name or "").strip()
    if not name:
        return None, "Give the node a name"
    if len(name) > NAME_MAX_LEN:
        return None, f"The node's name must be at most {NAME_MAX_LEN} characters"
    # The name starts every job-log line of the node: a line break would forge a second line, and a hidden or
    # right-to-left character would make it read as something else.
    if any(_hidden(ch) for ch in name):
        return None, "The node's name can't have line breaks, tabs or hidden characters"
    if not any(unicodedata.category(ch)[0] in "LNPS" for ch in name):
        return None, "Give the node a name"
    if any(name_key(str(o.get("name") or "").strip()) == name_key(name) for o in others):
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
    key_typed = raw.get("key") not in (None, KEY_MASK)
    if existing is not None and (url != normalise_url(base.get("url"))[0] or key_typed):
        # A new address may be another machine, and typing the key again is how the user confirms a Plex database
        # node that reports a new id: either way the id is recorded on its next answer.
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


def _record_label(entry: dict, fallback: str) -> str:
    """How an error names a stored record: its name, else ``fallback``."""
    name = entry.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else fallback


def _without_uid(entry: dict | None) -> dict | None:
    return None if entry is None else {k: v for k, v in entry.items() if k != "node_uid"}


def validate_nodes(raw: object, saved: list[dict] | None = None) -> tuple[list[dict] | None, str]:
    """Validate a whole ``nodes`` list as stored (each record keeps its id, uid and key).

    A record that isn't valid but is as saved (written into settings.json by hand) is kept as it is: it holds up only
    a change to itself, never a save of the other nodes. The id the node reports may still be recorded on it: the
    registry writes that by itself, and without it a Plex database node's requests never name the saved id.

    Args:
        raw: The list to store.
        saved: The list as it is saved now.

    Returns:
        ``(nodes, "")`` or ``(None, message)``; a message about one record names it.
    """
    if not isinstance(raw, list):
        return None, "nodes must be a list"
    as_saved = {str(n.get("id")): n for n in saved or [] if isinstance(n, dict) and n.get("id")}
    out: list[dict] = []
    seen_ids: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            return None, "Each node must be an object"
        if not str(entry.get("id") or ""):
            return None, f"{_record_label(entry, 'A node')} needs an id"
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
            if _without_uid(as_saved.get(node_id)) == _without_uid(entry):
                out.append(copy.deepcopy(entry))
                continue
            return None, f"{_record_label(entry, node_id)}: {err}"
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


def server_agent(entry: object) -> dict | None:
    """A Plex server entry's ``markers.plex.agent`` block (its Plex database node choice), or None."""
    if not isinstance(entry, dict) or str(entry.get("type") or "").lower() != "plex":
        return None
    markers = entry.get("markers")
    plex = markers.get("plex") if isinstance(markers, dict) else None
    agent = plex.get("agent") if isinstance(plex, dict) else None
    return agent if isinstance(agent, dict) else None


def node_users(
    node_id: str, worker_groups: object, media_servers: object, *, switched_on_only: bool = False
) -> dict[str, list[dict[str, str]]]:
    """The worker groups with a member on the node, and the Plex servers whose database it opens.

    A server whose Plex database node is switched off doesn't use it (its Intro & Credits tab says this app opens the
    database), even though it remembers the node for when it is switched on again. One switched on uses it for
    everything that writes Plex's database: markers, chapter thumbnails, loudness and Library Health's counts.

    Args:
        node_id: The node.
        worker_groups: settings.json's ``worker_groups``.
        media_servers: settings.json's ``media_servers``.
        switched_on_only: Leave out a worker group or Plex server that is switched off: nothing of theirs runs, so
            nothing waits on the node (the bell's rule). "Used by" and Remove count them, as they still point at it.

    Returns:
        ``{"groups": [{"id", "name"}], "servers": [{"id", "name"}]}``.
    """
    groups = []
    for group in worker_groups if isinstance(worker_groups, list) else []:
        if not isinstance(group, dict) or (switched_on_only and not group.get("enabled", True)):
            continue
        members = group.get("members") or []
        if any(isinstance(m, dict) and m.get("node") == node_id for m in members):
            groups.append({"id": str(group.get("id") or ""), "name": str(group.get("name") or "")})
    servers = []
    for entry in media_servers if isinstance(media_servers, list) else []:
        agent = server_agent(entry)
        if agent is None or not agent.get("enabled") or agent.get("node_id") != node_id:
            continue
        if switched_on_only and not entry.get("enabled", True):
            continue
        servers.append({"id": str(entry.get("id") or ""), "name": str(entry.get("name") or "")})
    return {"groups": groups, "servers": servers}


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
