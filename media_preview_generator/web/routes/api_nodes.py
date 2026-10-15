"""REST API behind the Nodes page (spec §10.4).

Keys never come back: a node's key is stored, sent to the node, and nothing else. Posting ``****`` keeps it, but only
for the address it was saved with: a stored key is never sent to an address someone just typed. Every route needs a
signed-in session or the API token, even before setup is complete (the setup wizard doesn't use them), because they
send stored keys out.
"""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime
from typing import Any

from flask import current_app, jsonify, request
from loguru import logger

from ...nodes import protocol
from ...nodes.client import (
    NodeAddress,
    NodeAuthError,
    NodeClient,
    NodeError,
    NodeProtocolError,
    NodeRequestError,
)
from ...nodes.mappings import default_mappings_for, library_roots, run_test_access
from ...nodes.readiness import node_readiness, to_fix
from ...nodes.registry import NodeDetails, _default_legacy_ping, get_registry
from ...nodes.settings import KEY_MASK, find_node, normalise_key, normalise_mappings, normalise_url, validate_node
from ..auth import api_token_required
from ..settings_manager import get_settings_manager
from . import api

LOG_LINES_MAX = 2000
# The probe error kind beyond the protocol's request-level ones: nothing answered.
KIND_UNREACHABLE = "unreachable"
# Why something that answered isn't a node (``error.reason`` next to the protocol kind).
PROBE_THIS_APP = "this_app"
PROBE_OTHER_APP = "other_app"
PROBE_WEB_PAGE = "web_page"
# Bounds on what a node sends that is passed on to the browser.
NODE_TEXT_MAX = 500
NODE_LIST_MAX = 64
NODE_DEPTH_MAX = 4
KEY_FOR_NEW_ADDRESS = "Type the node's key again: a saved key is only sent to the address it was saved with."


def _app_version() -> str:
    from ... import __version__

    return str(__version__)


def _make_client(url: str, key: str) -> NodeClient:
    return NodeClient(NodeAddress(url, key))


def _legacy_ping(url: str, key: str) -> tuple[str, str]:
    return _default_legacy_ping(url, key)


def _server_agent(entry: object) -> dict | None:
    """A Plex server entry's ``markers.plex.agent`` block, or None."""
    if not isinstance(entry, dict) or str(entry.get("type") or "").lower() != "plex":
        return None
    markers = entry.get("markers")
    plex = markers.get("plex") if isinstance(markers, dict) else None
    agent = plex.get("agent") if isinstance(plex, dict) else None
    return agent if isinstance(agent, dict) else None


def _used_by(node_id: str) -> dict[str, list[dict[str, str]]]:
    """The worker groups with a member on the node, and the Plex servers whose database it opens.

    A server whose Plex database node is switched off doesn't use it (its Intro & Credits tab says this app opens the
    database), even though it remembers the node for when it is switched on again.
    """
    sm = get_settings_manager()
    groups = []
    for group in sm.get("worker_groups") or []:
        if not isinstance(group, dict):
            continue
        members = group.get("members") or []
        if any(isinstance(m, dict) and m.get("node") == node_id for m in members):
            groups.append({"id": str(group.get("id") or ""), "name": str(group.get("name") or "")})
    servers = []
    for entry in sm.get("media_servers") or []:
        agent = _server_agent(entry)
        if agent is not None and agent.get("enabled") and agent.get("node_id") == node_id:
            servers.append({"id": str(entry.get("id") or ""), "name": str(entry.get("name") or "")})
    return {"groups": groups, "servers": servers}


def _finite(value: object) -> int | float | None:
    """A number a node sent, or None when it isn't a finite one (a ``NaN`` would make the answer invalid JSON)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value if math.isfinite(value) else None


def _node_sent(value: object, depth: int = 0) -> Any:
    """A JSON value a node sent, made safe to pass on to the browser.

    Text is cut to :data:`NODE_TEXT_MAX` characters, lists and objects to :data:`NODE_LIST_MAX` entries and
    :data:`NODE_DEPTH_MAX` levels; anything that isn't text, a finite number, a bool or null becomes null.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return _finite(value)
    if isinstance(value, str):
        return value[:NODE_TEXT_MAX]
    if depth >= NODE_DEPTH_MAX:
        return None
    if isinstance(value, dict):
        return {str(k)[:NODE_TEXT_MAX]: _node_sent(v, depth + 1) for k, v in list(value.items())[:NODE_LIST_MAX]}
    if isinstance(value, list | tuple):
        return [_node_sent(v, depth + 1) for v in value[:NODE_LIST_MAX]]
    return None


def _node_row(record: dict, details: NodeDetails | None) -> dict[str, Any]:
    """``NODE_ROW`` for one stored node and its live details (None before the registry has it)."""
    view = details.view if details else None
    cpu = details.cpu if details else {}
    return {
        "id": record["id"],
        "name": record.get("name", ""),
        "url": record.get("url", ""),
        "enabled": bool(record.get("enabled", True)),
        "state": view.state if view else "connecting",
        "reason": view.reason if view else "",
        "version": view.version if view else "",
        "roles": list(view.roles) if view else [],
        "legacy": bool(view.legacy) if view else False,
        "platform": _node_sent(details.platform) if details else {},
        "gpus": [_node_sent(dict(g)) for g in details.gpus] if details else [],
        "cpu": {key: _finite(cpu.get(key)) for key in ("cores", "load1", "ram_total", "ram_available")},
        "busy": details.busy if details else 0,
        "last_seen": view.last_seen if view else None,
        "path_mappings": list(record.get("path_mappings") or []),
        "used_by": _used_by(record["id"]),
        "to_fix": _to_fix(record, details),
    }


def _to_fix(record: dict, details: NodeDetails | None) -> dict[str, int]:
    """The Setup Health counts the card shows, so a "Ready" card still says when its Setup Health has rows to fix.

    The Plex database checks ask the node, so they are left out here; its Setup Health tab runs them.
    """
    if details is None:
        return {"must_fix": 0, "recommended": 0}
    sm = get_settings_manager()
    envelope = node_readiness(
        details,
        key=str(record.get("key") or ""),
        worker_groups=sm.get("worker_groups"),
        app_version=_app_version(),
        file_checks=None,
    )
    return to_fix(envelope)


def _row_for(node_id: str) -> dict[str, Any] | None:
    record = find_node(get_settings_manager().nodes, node_id)
    if record is None:
        return None
    return _node_row(record, get_registry().details(node_id))


def _not_found(node_id: str):
    return jsonify({"error": f"No node with id {node_id}"}), 404


def _same_address(url: str, record: dict) -> bool:
    """Whether a normalised address is the one the record was saved with."""
    return url == normalise_url(record.get("url"))[0]


def _probe_error(kind: str, message: str, reason: str = "") -> dict[str, Any]:
    """A failed probe; ``reason`` narrows ``kind`` down to what to check (Add node words each one)."""
    error = {"kind": kind, "message": message}
    if reason:
        error["reason"] = reason
    return {"ok": False, "error": error}


def _not_a_node(exc: NodeProtocolError) -> dict[str, Any] | None:
    """The probe's answer when the address is this app (or another copy of it) or a web page, else None."""
    if exc.app_instance:
        own = exc.app_instance == current_app.config.get("APP_INSTANCE_ID")
        return _probe_error(
            protocol.KIND_PROTOCOL,
            "This address is this app itself, not a node."
            if own
            else "This address is another copy of this app, not a node.",
            PROBE_THIS_APP if own else PROBE_OTHER_APP,
        )
    if exc.web_page:
        return _probe_error(protocol.KIND_PROTOCOL, "This address answers with a web page, not a node.", PROBE_WEB_PAGE)
    return None


def _credit_notes(gpus: list[dict]) -> list[dict[str, str]]:
    """One note per GPU whose credits text detection runs on the CPU (known once a credits task has run there)."""
    notes = []
    for gpu in gpus:
        credit = gpu.get("credit_text")
        if isinstance(credit, dict) and credit.get("on_gpu") is False:
            notes.append(
                {
                    "title": "Credits text",
                    "chip": f"CPU on {gpu.get('name') or gpu.get('device') or 'this GPU'}",
                    "message": str(credit.get("reason") or ""),
                }
            )
    return notes


def _probe_info(info: dict) -> dict[str, Any]:
    """The probe's ``info``: ``/v1/info`` projected to what Add node shows, plus whether the versions match."""
    block = info.get("node") if isinstance(info.get("node"), dict) else {}
    roles = [r for r in block.get("roles") or [] if r in protocol.ROLES]
    version = protocol.node_version(info)
    worker = info.get("worker") if isinstance(info.get("worker"), dict) else None
    gpus = [g for g in (worker or {}).get("gpus") or [] if isinstance(g, dict)]
    plex_db = info.get("plex_db")
    return _node_sent(
        {
            "node": {
                "id": str(block.get("id") or "") or None,
                "name": str(block.get("name") or ""),
                "version": version,
                "roles": roles,
            },
            "legacy": False,
            "platform": info.get("platform") if isinstance(info.get("platform"), dict) else {},
            "worker": None
            if worker is None
            else {
                "gpus": gpus,
                "cpu": worker.get("cpu") if isinstance(worker.get("cpu"), dict) else {},
                "temp": worker.get("temp") if isinstance(worker.get("temp"), dict) else {},
                "notes": _credit_notes(gpus),
            },
            "plex_db": plex_db if isinstance(plex_db, dict) else None,
            "version_match": (
                version == _app_version() and protocol.same_build(str(block.get("build") or ""), protocol.build_sha())
                if protocol.ROLE_WORKER in roles
                else None
            ),
        }
    )


def _probe(url: str, key: str) -> dict[str, Any]:
    """Ask an address what it is, without saving anything: ``{"ok", "info"}`` or ``{"ok": False, "error"}``."""
    from ...markers.publishers.plex_remote import AGENT_CONNECTED, AGENT_REJECTED

    try:
        info = _make_client(url, key).info()
    except NodeAuthError:
        return _probe_error(protocol.KIND_AUTH, "The node refused this key.")
    except NodeRequestError as exc:
        return _probe_error(protocol.KIND_REQUEST, str(exc))
    except NodeProtocolError as exc:
        if exc.node is not None:
            node_version = str(exc.node.get("version") or "")[:NODE_TEXT_MAX] or "unknown"
            answer = _probe_error(
                protocol.KIND_PROTOCOL,
                f"This node runs version {node_version} and this app runs {_app_version()}, and they don't speak the "
                "same node protocol. Update the older one so both run the same version.",
            )
            answer["error"]["node_version"] = node_version
            return answer
        if exc.app_instance:
            return _not_a_node(exc)
        state, version = _legacy_ping(url, key)
        if state == AGENT_REJECTED:
            return _probe_error(protocol.KIND_AUTH, "The Plex database node refused this key.")
        if state != AGENT_CONNECTED:
            return _not_a_node(exc) or _probe_error(
                protocol.KIND_PROTOCOL,
                "Something answered at this address, but not as a node this app can use.",
            )
        return {
            "ok": True,
            "info": {
                "node": {"id": None, "name": "", "version": version, "roles": [protocol.ROLE_PLEX_DB]},
                "legacy": True,
                "platform": {},
                "worker": None,
                "plex_db": None,
                "version_match": None,
            },
        }
    except NodeError as exc:
        return _probe_error(KIND_UNREACHABLE, str(exc), getattr(exc, "reason", ""))
    return {"ok": True, "info": _probe_info(info)}


def _log_line(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {"time": "", "level": "", "message": str(raw)}
    line = {k: str(raw.get(k) or "") for k in ("time", "level", "message")}
    line["time"] = _in_app_time(raw.get("ts"), line["time"])
    return line


def _in_app_time(ts: object, node_time: str) -> str:
    """A node log line's moment in this app's time zone (as the app's own log reads), ISO 8601 with its offset.

    ``ts`` is epoch seconds; a node older than ``ts`` sends only ``time`` with its own offset, read instead. A time
    that can't be read is passed on as the node wrote it.
    """
    when = None
    if isinstance(ts, int | float) and not isinstance(ts, bool) and math.isfinite(ts):
        when = datetime.fromtimestamp(ts, UTC)
    else:
        try:
            parsed = datetime.fromisoformat(node_time)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            when = parsed
    return when.astimezone().isoformat(timespec="seconds") if when is not None else node_time


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@api.route("/nodes", methods=["GET"])
@api_token_required
def list_nodes():
    """Every node as ``NODE_ROW`` and this app's version."""
    sm = get_settings_manager()
    registry = get_registry()
    rows = [_node_row(record, registry.details(record["id"])) for record in sm.nodes if record.get("id")]
    return jsonify({"nodes": rows, "app_version": _app_version()})


@api.route("/nodes/probe", methods=["POST"])
@api_token_required
def probe_node():
    """Add node step 1: what answers at this address with this key. Nothing is saved."""
    data = _json_body()
    url, err = normalise_url(data.get("url"))
    if url is None:
        return jsonify({"error": err}), 400
    sm = get_settings_manager()
    record = find_node(sm.nodes, data.get("id")) if data.get("id") else None
    stored = ""
    if record and data.get("key") == KEY_MASK:
        if not _same_address(url, record):
            return jsonify({"error": KEY_FOR_NEW_ADDRESS}), 400
        stored = str(record.get("key") or "")
    key, err = normalise_key(data.get("key"), stored)
    if key is None:
        return jsonify({"error": err}), 400
    result = _probe(url, key)
    if not result["ok"]:
        return jsonify(result)
    info = result["info"]
    uid = info["node"]["id"]
    own_id = record["id"] if record else None
    twin = next((n for n in sm.nodes if uid and n.get("node_uid") == uid and n["id"] != own_id), None)
    info["already_added"] = {"id": twin["id"], "name": twin.get("name", "")} if twin else None
    servers = sm.get("media_servers") or []
    info["suggested_path_mappings"] = (
        default_mappings_for(servers) if protocol.ROLE_WORKER in info["node"]["roles"] else []
    )
    info["media_setup"] = _media_setup(servers)
    return jsonify(result)


def _media_setup(servers: list[dict]) -> dict[str, int]:
    """How many media servers are on and how many libraries they list, so an empty list of rows can say which is missing.

    Args:
        servers: The stored ``media_servers`` list.

    Returns:
        ``{"servers": n, "libraries": n}``.
    """
    on = [s for s in servers if isinstance(s, dict) and s.get("enabled", True)]
    libraries = {(root.server_id, root.library_id) for root in library_roots(on)}
    return {"servers": len(on), "libraries": len(libraries)}


def _probed_uid(url: str, key: str) -> str | None:
    """The id of the node answering at an address with this key, or None when none answers as a node."""
    probe = _probe(url, key)
    return probe["info"]["node"]["id"] if probe["ok"] else None


def _twin(nodes: list[dict], url: str, uid: str | None) -> dict | None:
    """The saved node at this address, or the one whose id the node answering there reported."""
    return next((n for n in nodes if _same_address(url, n) or (uid and n.get("node_uid") == uid)), None)


def _already_added(twin: dict) -> tuple[Any, int]:
    answer = {"error": f"This node is already added as {twin.get('name')}", "node_id": twin["id"]}
    return jsonify({**answer, "name": twin.get("name", "")}), 409


@api.route("/nodes", methods=["POST"])
@api_token_required
def create_node():
    """Add a node. 409 when the node answering at that address is already added."""
    data = _json_body()
    sm = get_settings_manager()
    record, err = validate_node({**data, "node_uid": None}, existing=None, others=sm.nodes)
    if record is None:
        return jsonify({"error": err}), 400
    uid = _probed_uid(record["url"], record["key"])
    with sm.locked():
        nodes = sm.nodes
        twin = _twin(nodes, record["url"], uid)
        if twin is not None:
            return _already_added(twin)
        others_err = validate_node(record, existing=None, others=nodes)[1]
        if others_err:
            return jsonify({"error": others_err}), 400
        record["node_uid"] = uid
        nodes.append(record)
        try:
            sm.update_nodes(nodes)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    logger.info("Added node {} at {}", record["name"], record["url"])
    get_registry().report_unavailable(record["id"])
    return jsonify(_row_for(record["id"])), 201


@api.route("/nodes/<node_id>", methods=["PUT"])
@api_token_required
def update_node(node_id: str):
    """Change a node's name, address, key, on/off switch or mappings."""
    data = {k: v for k, v in _json_body().items() if k in ("name", "url", "key", "enabled", "path_mappings")}
    sm = get_settings_manager()
    before = find_node(sm.nodes, node_id)
    new_url = normalise_url(data["url"])[0] if "url" in data else None
    moving = before is not None and new_url is not None and not _same_address(new_url, before)
    typed_key = normalise_key(data.get("key"), "")[0] if data.get("key") not in (None, KEY_MASK) else None
    # Asked before the lock (it waits on the network); a node at the new address may already be added.
    uid = _probed_uid(new_url, typed_key) if moving and new_url and typed_key else None
    with sm.locked():
        nodes = sm.nodes
        existing = find_node(nodes, node_id)
        if existing is None:
            return _not_found(node_id)
        if "url" in data and data.get("key") in (None, KEY_MASK):
            new_url = normalise_url(data["url"])[0]
            if new_url is not None and not _same_address(new_url, existing):
                return jsonify({"error": KEY_FOR_NEW_ADDRESS}), 400
        others = [n for n in nodes if n is not existing]
        record, err = validate_node(data, existing=existing, others=others)
        if record is None:
            return jsonify({"error": err}), 400
        if not _same_address(record["url"], existing):
            twin = _twin(others, record["url"], uid)
            if twin is not None:
                return _already_added(twin)
        reconnect = any(record[k] != existing.get(k) for k in ("url", "key", "path_mappings", "enabled"))
        nodes[nodes.index(existing)] = record
        try:
            sm.update_nodes(nodes)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    if reconnect:
        get_registry().report_unavailable(node_id)
    return jsonify(_row_for(node_id))


def _forget_switched_off_choice(sm: Any, node_id: str) -> None:
    """Drop a removed node from the Plex servers that remembered it with their Plex database node switched off, so
    switching one on again asks for a node instead of pointing at one that is gone."""
    servers = copy.deepcopy(sm.get("media_servers") or [])
    changed = False
    for entry in servers:
        agent = _server_agent(entry)
        if agent is not None and agent.get("node_id") == node_id:
            agent["node_id"] = None
            changed = True
    if changed:
        sm.set("media_servers", servers)


@api.route("/nodes/<node_id>", methods=["DELETE"])
@api_token_required
def delete_node(node_id: str):
    """Remove a node; refused while a worker group member or a Plex server uses it."""
    sm = get_settings_manager()
    with sm.locked():
        nodes = sm.nodes
        existing = find_node(nodes, node_id)
        if existing is None:
            return _not_found(node_id)
        used = _used_by(node_id)
        used_by = [{"kind": "group", **g} for g in used["groups"]] + [{"kind": "server", **s} for s in used["servers"]]
        if used_by:
            names = ", ".join(u["name"] or u["id"] for u in used_by)
            return jsonify({"error": f"{existing.get('name')} is still used by {names}", "used_by": used_by}), 409
        try:
            sm.update_nodes([n for n in nodes if n is not existing])
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        _forget_switched_off_choice(sm, node_id)
    logger.info("Removed node {}", existing.get("name"))
    return jsonify({"ok": True})


@api.route("/nodes/<node_id>/check", methods=["POST"])
@api_token_required
def check_node(node_id: str):
    """Run a health check now and answer with the fresh ``NODE_ROW``."""
    if get_registry().check_now(node_id) is None:
        return _not_found(node_id)
    return jsonify(_row_for(node_id))


@api.route("/nodes/<node_id>/test-access", methods=["POST"])
@api_token_required
def test_node_access(node_id: str):
    """Sample every enabled library and ask the node to find each file (saved mappings, or the posted ones)."""
    sm = get_settings_manager()
    record = find_node(sm.nodes, node_id)
    if record is None:
        return _not_found(node_id)
    data = _json_body()
    stored = record.get("path_mappings") or []
    rows, err = normalise_mappings(data["path_mappings"]) if "path_mappings" in data else (stored, "")
    if rows is None:
        return jsonify({"error": err}), 400
    mappings = [protocol.PathMapping.from_json(r) for r in rows]
    try:
        result = run_test_access(
            _make_client(record["url"], record["key"]),
            mappings,
            sm.get("media_servers") or [],
            node_name=str(record.get("name") or "This node"),
        )
    except NodeError as exc:
        return jsonify({"error": f"Couldn't ask {record.get('name')}: {exc}"}), 502
    result["media_setup"] = _media_setup(sm.get("media_servers") or [])
    # The registry keeps a test of rows not saved yet aside, for when exactly those rows are saved.
    get_registry().record_access_result(node_id, result, tested_mappings=rows, tested_url=record["url"])
    return jsonify(result)


@api.route("/nodes/<node_id>/suggested-mappings", methods=["GET"])
@api_token_required
def suggested_node_mappings(node_id: str):
    """Rows to pre-fill for a node with none saved: one per library folder, the same path on the node.

    A node added before any library was loaded got no rows in Add node; its Media tab fills them in later from this.
    """
    sm = get_settings_manager()
    if find_node(sm.nodes, node_id) is None:
        return _not_found(node_id)
    servers = sm.get("media_servers") or []
    return jsonify({"path_mappings": default_mappings_for(servers), "media_setup": _media_setup(servers)})


@api.route("/nodes/<node_id>/health", methods=["GET"])
@api_token_required
def node_health(node_id: str):
    """The node's Setup Health envelope."""
    sm = get_settings_manager()
    record = find_node(sm.nodes, node_id)
    details = get_registry().details(node_id)
    if record is None or details is None:
        return _not_found(node_id)
    envelope = node_readiness(
        details,
        key=str(record.get("key") or ""),
        worker_groups=sm.get("worker_groups"),
        app_version=_app_version(),
        app_build=protocol.build_sha(),
    )
    return jsonify(envelope)


@api.route("/nodes/<node_id>/log", methods=["GET"])
@api_token_required
def node_log(node_id: str):
    """The node's own last N log lines, oldest first, as ``{time, level, message}``."""
    record = find_node(get_settings_manager().nodes, node_id)
    if record is None:
        return _not_found(node_id)
    try:
        lines = max(1, min(LOG_LINES_MAX, int(request.args.get("lines", 200))))
    except ValueError:
        return jsonify({"error": "lines must be a number"}), 400
    try:
        answer = _make_client(record["url"], record["key"]).log(lines)
    except NodeError as exc:
        return jsonify({"error": f"Couldn't read {record.get('name')}'s log: {exc}"}), 502
    raw = answer.get("lines")
    return jsonify({"lines": [_log_line(line) for line in raw] if isinstance(raw, list) else []})


@api.route("/nodes/<node_id>/rescan-gpus", methods=["POST"])
@api_token_required
def rescan_node_gpus(node_id: str):
    """Ask the node to detect its GPUs again and answer with the fresh ``NODE_ROW`` (502 when it can't be asked)."""
    try:
        view = get_registry().rescan_gpus(node_id)
    except NodeError as exc:
        record = find_node(get_settings_manager().nodes, node_id) or {}
        return jsonify({"error": f"Couldn't ask {record.get('name') or 'the node'}: {exc}"}), 502
    if view is None:
        return _not_found(node_id)
    return jsonify(_row_for(node_id))
