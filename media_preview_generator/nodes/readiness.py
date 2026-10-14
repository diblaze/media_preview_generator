"""Setup Health for one node: the same envelope and buckets as a server's Setup Health card.

``{"overall_ok", "sections": [{"id", "title", "docs_anchor", "ok", "severity", "checks": [row...]}]}``. A row is
"must fix" (``critical``) when it stops the node doing work, "recommended" otherwise.

A failing row says where its fix is: ``fix_tab`` (``media`` / ``settings`` / ``overview``, the node dialog tab, with
``fix_label`` naming the button and, on media rows, the ``suggestion`` {from, to, app_root} the button applies),
``fix_where: "node"`` (only the node's own machine can fix it), or ``help_url`` + ``help_label`` (a page of this app).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..markers.readiness import CRITICAL, RECOMMENDED
from ..markers.readiness import _row as readiness_row
from . import protocol
from .registry import (
    STATE_ACCESS_PROBLEM,
    STATE_CONNECTING,
    STATE_DISABLED,
    STATE_KEY_REFUSED,
    STATE_OFFLINE,
    STATE_UPDATE_NEEDED,
    NodeDetails,
)

DOCS_ANCHOR = "nodes"
PLEX_DB_CHECK_S = 10.0
FIX_ON_NODE = "node"
TAB_MEDIA = "media"
TAB_SETTINGS = "settings"
WORKERS_URL = "/settings#section-workers"
# Where this app's own path mappings and library checks live.
SERVERS_URL = "/servers"
# Node-only row keys; the shared row builder doesn't know them.
_NODE_KEYS = ("fix_tab", "suggestion", "help_url", "help_label")


def _row(
    check_id: str, label: str, *, severity: str, ok: bool, explanation: str, tooltip: str | None = None, **extra: Any
) -> dict[str, Any]:
    node_keys = {k: extra.pop(k) for k in _NODE_KEYS if k in extra}
    row = readiness_row(
        check_id,
        label,
        severity=severity,
        ok=ok,
        tooltip=tooltip or explanation,
        explanation=explanation,
        docs_anchor=DOCS_ANCHOR,
        **extra,
    )
    # A passing row has nothing to fix, so it carries no fix pointer.
    if not ok:
        row.update({k: v for k, v in node_keys.items() if v is not None})
        if row.get("fix_where") is None:
            row.pop("fix_where", None)
    else:
        row.pop("fix_where", None)
        row.pop("fix_label", None)
        row.pop("fix_action", None)
    return row


def _section(section_id: str, title: str, checks: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not checks:
        return None
    failing_critical = any(c["ok"] is False and c["severity"] == CRITICAL for c in checks)
    return {
        "id": section_id,
        "title": title,
        "docs_anchor": DOCS_ANCHOR,
        "ok": all(c["ok"] for c in checks),
        "severity": CRITICAL if failing_critical else RECOMMENDED,
        "checks": checks,
    }


def _connection(details: NodeDetails, app_version: str) -> list[dict[str, Any]]:
    view = details.view
    if view.state == STATE_DISABLED:
        return [
            _row(
                "node_disabled",
                f"{view.name} is turned off",
                severity=RECOMMENDED,
                ok=True,
                explanation="Nothing is checked or sent to it until you turn it on.",
            )
        ]
    reachable = view.state not in (STATE_OFFLINE, STATE_CONNECTING)
    rows = [
        _row(
            "node_reachable",
            f"{view.name} answers" if reachable else f"{view.name} isn't answering",
            severity=CRITICAL,
            ok=reachable,
            explanation="The app checks every node every 30 seconds.",
            reason=None if reachable else view.reason,
            fix_tab=TAB_SETTINGS,
            fix_label="Check the address",
        )
    ]
    if not reachable:
        return rows
    key_ok = view.state != STATE_KEY_REFUSED
    rows.append(
        _row(
            "node_key",
            "The node accepts this app's key" if key_ok else "The node refused this app's key",
            severity=CRITICAL,
            ok=key_ok,
            explanation="The node only works for an app that sends its key.",
            reason=None if key_ok else view.reason,
            fix_tab=TAB_SETTINGS,
            fix_label="Change the key",
        )
    )
    if not key_ok:
        return rows
    version_ok = view.state != STATE_UPDATE_NEEDED
    rows.append(
        _row(
            "node_version",
            "The node's version matches this app" if version_ok else "The node needs an update",
            severity=CRITICAL,
            ok=version_ok,
            explanation="A worker node runs this app's own code, so it must be the same version.",
            current=view.version or "unknown",
            recommended=app_version if protocol.ROLE_WORKER in view.roles else None,
            reason=None if version_ok else view.reason,
            fix_where=FIX_ON_NODE,
        )
    )
    if view.legacy:
        rows.append(
            _row(
                "node_legacy_helper",
                "This is an older Plex helper",
                severity=RECOMMENDED,
                ok=False,
                explanation="It keeps working for Plex's database. Updating it to a node adds health details and "
                "the newer Plex database features.",
                reason="Update it to the Plex database node image.",
                fix_where=FIX_ON_NODE,
            )
        )
    return rows


def _media_access(details: NodeDetails) -> list[dict[str, Any]]:
    view = details.view
    rows: list[dict[str, Any]] = []
    if view.state == STATE_ACCESS_PROBLEM:
        rows.append(
            _row(
                "node_access_problem",
                f"{view.name} couldn't see files it was sent",
                severity=CRITICAL,
                ok=False,
                explanation="Its workers are paused until a Test access passes.",
                reason=view.reason,
                fix_tab=TAB_MEDIA,
                fix_label="Test access",
                fix_action="test_access",
            )
        )
    result = details.access_result
    if not isinstance(result, dict):
        rows.append(
            _row(
                "node_access_untested",
                "Run Test access",
                severity=RECOMMENDED,
                ok=False,
                explanation="Test access checks a few files from every library through this node's folder mappings.",
                fix_tab=TAB_MEDIA,
                fix_label="Test access",
                fix_action="test_access",
            )
        )
        return rows
    for index, lib in enumerate(result.get("libraries") or []):
        if not isinstance(lib, dict):
            continue
        label = f"{lib.get('server')} · {lib.get('library')}"
        if not lib.get("checked"):
            # Nothing was sampled: this app can't open the folder itself or found no video in it, so the node was
            # never asked. Blaming the node (critical, "Open Media") would send the user to the wrong machine.
            rows.append(
                _row(
                    f"node_access_{index}",
                    f"{label}: not tested, this app can't read it itself",
                    severity=RECOMMENDED,
                    ok=False,
                    explanation="Test access needs a file this app sees to ask the node for. Fix this app's own "
                    "access to the library first; this row says nothing about the node.",
                    reason=lib.get("error"),
                    help_url=SERVERS_URL,
                    help_label="Open Media Servers",
                )
            )
            continue
        ok = bool(lib.get("ok"))
        missing = [f for f in lib.get("files") or [] if isinstance(f, dict) and not f.get("ok")]
        reason = None
        if not ok:
            reason = lib.get("error") or (missing[0].get("message") if missing else None)
        suggestion = lib.get("suggestion") if isinstance(lib.get("suggestion"), dict) else None
        rows.append(
            _row(
                f"node_access_{index}",
                f"{label}: the node sees its files" if ok else f"{label}: the node can't see its files",
                severity=CRITICAL,
                ok=ok,
                explanation=f"Checked {lib.get('checked', 0)} file(s) under {lib.get('app_root')}.",
                current=suggestion.get("from") if suggestion else None,
                recommended=suggestion.get("to") if suggestion else None,
                reason=reason,
                fix_tab=TAB_MEDIA,
                fix_label="Use the suggested path" if suggestion else "Open Media",
                suggestion={**suggestion, "app_root": lib.get("app_root")} if suggestion else None,
            )
        )
    return rows


def _members_using(node_id: str, groups: object) -> int:
    if not isinstance(groups, list):
        return 0
    return sum(
        1
        for group in groups
        if isinstance(group, dict)
        for member in group.get("members") or []
        if isinstance(member, dict) and member.get("node") == node_id
    )


def _workers(details: NodeDetails, worker_groups: object) -> list[dict[str, Any]]:
    view = details.view
    used = _members_using(view.id, worker_groups) > 0
    rows = [
        _row(
            "node_in_group",
            "Used in a worker group" if used else "Not used in any worker group yet",
            severity=RECOMMENDED,
            ok=used,
            explanation="A node runs work only for the worker groups it is a member of.",
            reason=None if used else "Add this node's GPU or CPU to a worker group on the Workers page.",
            help_url=WORKERS_URL,
            help_label="Open Workers",
        )
    ]
    for gpu in view.gpus:
        ok = gpu.get("status") == "ok"
        name = gpu.get("name") or gpu.get("device") or "GPU"
        rows.append(
            _row(
                f"node_gpu_{gpu.get('device')}",
                f"{name} works" if ok else f"{name} failed its test",
                severity=CRITICAL,
                ok=ok,
                explanation="The node tests each GPU when it starts, every 5 minutes, and after a GPU error.",
                current=gpu.get("status") or "unknown",
                recommended="ok",
                fix_where=FIX_ON_NODE,
            )
        )
    return rows


def _default_file_checks(url: str, key: str) -> Any:
    from ..markers.publishers.plex_remote import RemotePlexDb

    return RemotePlexDb(url, key).file_checks(deadline=time.monotonic() + PLEX_DB_CHECK_S)


def _plex_db(url: str, key: str, file_checks: Callable[[str, str], Any]) -> list[dict[str, Any]]:
    from ..markers.publishers.base import Capability

    report = file_checks(url, key)
    details = report.details or {}
    if report.state is Capability.AGENT_UNAVAILABLE:
        return [
            _row(
                "plex_db_reachable",
                "The Plex database role can't be used",
                severity=CRITICAL,
                ok=False,
                explanation="The node opens Plex's database on Plex's own machine.",
                reason=report.message,
                fix_where=FIX_ON_NODE,
            )
        ]
    present = "db_path" in details
    rows = [
        _row(
            "plex_db_present",
            "Plex's database is found" if present else "Plex's database isn't found",
            severity=CRITICAL,
            ok=present,
            explanation="The node looks for it in the Plex folder it was started with.",
            current=details.get("db_path"),
            reason=None if present else report.message,
            fix_where=FIX_ON_NODE,
        )
    ]
    if not present:
        return rows
    local = report.state is not Capability.NEEDS_LOCAL_DB
    rows.append(
        _row(
            "plex_db_local",
            "It is on a local disk" if local else "It isn't on a local disk",
            severity=CRITICAL,
            ok=local,
            explanation="SQLite can only be written safely on the disk Plex itself uses.",
            current=details.get("fs_type"),
            reason=None if local else report.message,
            fix_where=FIX_ON_NODE,
        )
    )
    if not local:
        return rows
    writable = not details.get("unwritable")
    rows.append(
        _row(
            "plex_db_writable",
            "The node can write it" if writable else "The node can't write it",
            severity=CRITICAL,
            ok=writable,
            explanation="Markers and chapters are written into Plex's database.",
            reason=None if writable else report.message,
            fix_where=FIX_ON_NODE,
        )
    )
    if not writable:
        return rows
    held = details.get("lock_holder") is True
    rows.append(
        _row(
            "plex_db_held",
            "Plex has it open" if held else "Plex doesn't have it open",
            severity=CRITICAL,
            ok=held,
            explanation="The node writes only while Plex is running from the same folder.",
            reason=None if held else report.message,
            fix_where=FIX_ON_NODE,
        )
    )
    return rows


def node_readiness(
    details: NodeDetails,
    *,
    key: str,
    worker_groups: object,
    app_version: str,
    file_checks: Callable[[str, str], Any] = _default_file_checks,
) -> dict[str, Any]:
    """The Setup Health envelope for one node.

    Args:
        details: The node's registry details.
        key: The node's stored key (for the Plex database file checks).
        worker_groups: The stored ``worker_groups`` list.
        app_version: This app's version.
        file_checks: ``(url, key) -> CapabilityReport`` for the Plex database role (tests pass their own).

    Returns:
        ``{"overall_ok", "sections"}``.
    """
    view = details.view
    sections = [_section("node_connection", "Connection", _connection(details, app_version))]
    usable = view.state not in (STATE_DISABLED, STATE_OFFLINE, STATE_CONNECTING, STATE_KEY_REFUSED)
    worker = protocol.ROLE_WORKER in view.roles or not view.roles
    if usable and worker and not view.legacy:
        sections.append(_section("node_media_access", "Media access", _media_access(details)))
        sections.append(_section("node_workers", "Workers", _workers(details, worker_groups)))
    if usable and protocol.ROLE_PLEX_DB in view.roles:
        sections.append(_section("node_plex_db", "Plex database", _plex_db(view.url, key, file_checks)))
    kept = [s for s in sections if s is not None]
    overall_ok = not any(c["severity"] == CRITICAL and c["ok"] is False for s in kept for c in s["checks"])
    return {"overall_ok": overall_ok, "sections": kept}
