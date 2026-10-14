"""REST API behind the Nodes page (spec §10.4).

Keys never come back: a node's key is stored, sent to the node, and nothing else. Posting ``****`` keeps it.
"""

from __future__ import annotations

from typing import Any

from flask import jsonify, request
from loguru import logger

from ...nodes import protocol
from ...nodes.client import NodeAddress, NodeAuthError, NodeClient, NodeError, NodeProtocolError
from ...nodes.mappings import default_mappings_for, run_test_access
from ...nodes.readiness import node_readiness
from ...nodes.registry import NodeDetails, _default_legacy_ping, get_registry
from ...nodes.settings import KEY_MASK, find_node, normalise_key, normalise_mappings, normalise_url, validate_node
from ..auth import setup_or_auth_required
from ..settings_manager import get_settings_manager
from . import api

LOG_LINES_MAX = 2000


def _app_version() -> str:
    from ... import __version__

    return str(__version__)


def _make_client(url: str, key: str) -> NodeClient:
    return NodeClient(NodeAddress(url, key))


def _legacy_ping(url: str, key: str) -> tuple[str, str]:
    return _default_legacy_ping(url, key)


def _used_by(node_id: str) -> dict[str, list[dict[str, str]]]:
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
        if not isinstance(entry, dict) or str(entry.get("type") or "").lower() != "plex":
            continue
        markers = entry.get("markers")
        plex = markers.get("plex") if isinstance(markers, dict) else None
        agent = plex.get("agent") if isinstance(plex, dict) else None
        if isinstance(agent, dict) and agent.get("node_id") == node_id:
            servers.append({"id": str(entry.get("id") or ""), "name": str(entry.get("name") or "")})
    return {"groups": groups, "servers": servers}


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
        "platform": details.platform if details else {},
        "gpus": [dict(g) for g in view.gpus] if view else [],
        "cpu": {
            "cores": cpu.get("cores"),
            "load1": cpu.get("load1"),
            "ram_total": cpu.get("ram_total"),
            "ram_available": cpu.get("ram_available"),
        },
        "busy": details.busy if details else False,
        "last_seen": view.last_seen if view else None,
        "path_mappings": list(record.get("path_mappings") or []),
        "used_by": _used_by(record["id"]),
    }


def _row_for(node_id: str) -> dict[str, Any] | None:
    record = find_node(get_settings_manager().nodes, node_id)
    if record is None:
        return None
    return _node_row(record, get_registry().details(node_id))


def _not_found(node_id: str):
    return jsonify({"error": f"No node with id {node_id}"}), 404


def _probe(url: str, key: str) -> dict[str, Any]:
    """Ask an address what it is, without saving anything: ``{"ok", "info"}`` or ``{"ok": False, "error"}``."""
    from ...markers.publishers.plex_remote import AGENT_CONNECTED, AGENT_REJECTED

    try:
        info = _make_client(url, key).info()
    except NodeAuthError:
        return {"ok": False, "error": {"kind": protocol.KIND_AUTH, "message": "The node refused this key."}}
    except NodeProtocolError:
        state, version = _legacy_ping(url, key)
        if state == AGENT_REJECTED:
            return {"ok": False, "error": {"kind": protocol.KIND_AUTH, "message": "The Plex helper refused this key."}}
        if state != AGENT_CONNECTED:
            return {
                "ok": False,
                "error": {
                    "kind": protocol.KIND_PROTOCOL,
                    "message": "Something answered at this address, but not as a node or a Plex helper this app "
                    "can use.",
                },
            }
        return {
            "ok": True,
            "info": {
                "node_uid": None,
                "name": "",
                "version": version,
                "roles": [protocol.ROLE_PLEX_DB],
                "gpus": [],
                "platform": {},
                "legacy": True,
                "version_match": None,
            },
        }
    except NodeError as exc:
        return {"ok": False, "error": {"kind": "unreachable", "message": str(exc)}}
    block = info.get("node") if isinstance(info.get("node"), dict) else {}
    worker = info.get("worker") if isinstance(info.get("worker"), dict) else {}
    roles = [r for r in block.get("roles") or [] if r in protocol.ROLES]
    version = str(block.get("version") or "")
    return {
        "ok": True,
        "info": {
            "node_uid": str(block.get("id") or "") or None,
            "name": str(block.get("name") or ""),
            "version": version,
            "roles": roles,
            "gpus": [g for g in worker.get("gpus") or [] if isinstance(g, dict)],
            "platform": info.get("platform") if isinstance(info.get("platform"), dict) else {},
            "legacy": False,
            "version_match": version == _app_version() if protocol.ROLE_WORKER in roles else None,
        },
    }


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@api.route("/nodes", methods=["GET"])
@setup_or_auth_required
def list_nodes():
    """Every node as ``NODE_ROW``, this app's version, and pre-filled mappings for Add node."""
    sm = get_settings_manager()
    registry = get_registry()
    rows = [_node_row(record, registry.details(record["id"])) for record in sm.nodes if record.get("id")]
    return jsonify(
        {
            "nodes": rows,
            "app_version": _app_version(),
            "default_mappings": default_mappings_for(sm.get("media_servers") or []),
        }
    )


@api.route("/nodes/probe", methods=["POST"])
@setup_or_auth_required
def probe_node():
    """Add node step 1: what answers at this address with this key. Nothing is saved."""
    data = _json_body()
    url, err = normalise_url(data.get("url"))
    if url is None:
        return jsonify({"error": err}), 400
    stored = ""
    if data.get("key") == KEY_MASK and data.get("id"):
        record = find_node(get_settings_manager().nodes, data.get("id"))
        stored = str(record.get("key") or "") if record else ""
    key, err = normalise_key(data.get("key"), stored)
    if key is None:
        return jsonify({"error": err}), 400
    result = _probe(url, key)
    if result["ok"] and result["info"]["node_uid"]:
        twin = next((n for n in get_settings_manager().nodes if n.get("node_uid") == result["info"]["node_uid"]), None)
        result["info"]["already_added"] = twin["id"] if twin else None
    return jsonify(result)


@api.route("/nodes", methods=["POST"])
@setup_or_auth_required
def create_node():
    """Add a node. 409 when the node answering at that address is already added."""
    data = _json_body()
    sm = get_settings_manager()
    record, err = validate_node({**data, "node_uid": None}, existing=None, others=sm.nodes)
    if record is None:
        return jsonify({"error": err}), 400
    probe = _probe(record["url"], record["key"])
    uid = probe["info"]["node_uid"] if probe["ok"] else None
    with sm.locked():
        nodes = sm.nodes
        twin = next((n for n in nodes if uid and n.get("node_uid") == uid), None)
        if twin is not None:
            return jsonify({"error": f"This node is already added as {twin.get('name')}", "node_id": twin["id"]}), 409
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
@setup_or_auth_required
def update_node(node_id: str):
    """Change a node's name, address, key, on/off switch or mappings."""
    data = {k: v for k, v in _json_body().items() if k in ("name", "url", "key", "enabled", "path_mappings")}
    sm = get_settings_manager()
    with sm.locked():
        nodes = sm.nodes
        existing = find_node(nodes, node_id)
        if existing is None:
            return _not_found(node_id)
        others = [n for n in nodes if n is not existing]
        record, err = validate_node(data, existing=existing, others=others)
        if record is None:
            return jsonify({"error": err}), 400
        reconnect = any(record[k] != existing.get(k) for k in ("url", "key", "path_mappings", "enabled"))
        nodes[nodes.index(existing)] = record
        try:
            sm.update_nodes(nodes)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    if reconnect:
        get_registry().report_unavailable(node_id)
    return jsonify(_row_for(node_id))


@api.route("/nodes/<node_id>", methods=["DELETE"])
@setup_or_auth_required
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
        sm.update_nodes([n for n in nodes if n is not existing])
    logger.info("Removed node {}", existing.get("name"))
    return jsonify({"ok": True})


@api.route("/nodes/<node_id>/check", methods=["POST"])
@setup_or_auth_required
def check_node(node_id: str):
    """Run a health check now and answer with the fresh ``NODE_ROW``."""
    if get_registry().check_now(node_id) is None:
        return _not_found(node_id)
    return jsonify(_row_for(node_id))


@api.route("/nodes/<node_id>/test-access", methods=["POST"])
@setup_or_auth_required
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
        result = run_test_access(_make_client(record["url"], record["key"]), mappings, sm.get("media_servers") or [])
    except NodeError as exc:
        return jsonify({"error": f"Couldn't ask {record.get('name')}: {exc}"}), 502
    # Only a test of the saved mappings says anything about how the node runs work now.
    if rows == stored:
        get_registry().record_access_result(node_id, result)
    return jsonify(result)


@api.route("/nodes/<node_id>/health", methods=["GET"])
@setup_or_auth_required
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
    )
    return jsonify(envelope)


@api.route("/nodes/<node_id>/log", methods=["GET"])
@setup_or_auth_required
def node_log(node_id: str):
    """The node's own last N log lines."""
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
    return jsonify({"lines": [str(line) for line in raw] if isinstance(raw, list) else []})


@api.route("/nodes/<node_id>/rescan-gpus", methods=["POST"])
@setup_or_auth_required
def rescan_node_gpus(node_id: str):
    """Ask the node to detect its GPUs again and answer with the fresh ``NODE_ROW``."""
    if get_registry().rescan_gpus(node_id) is None:
        return _not_found(node_id)
    return jsonify(_row_for(node_id))
