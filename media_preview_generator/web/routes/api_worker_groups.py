"""One persisted worker policy for settings, quick scaling and setup."""

from __future__ import annotations

from datetime import datetime

from flask import jsonify, request
from loguru import logger

from ...job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_LOUDNESS, JOB_KIND_PREVIEWS
from ...worker_groups import (
    MAX_CPU_WORKERS,
    MAX_GPU_WORKERS,
    application_timezone,
    configured_group_totals,
    future_capacity,
    group_is_available,
    next_group_opening,
)
from ..auth import setup_or_auth_required
from ..settings_manager import WorkerGroupsConflict, get_settings_manager
from . import api


def future_job_capacity(settings, kind: str) -> int:
    """Count configured compatible capacity with some time outside global quiet hours."""
    return future_capacity(settings.worker_groups, settings.get("quiet_hours"), kind)


def reconcile_group_settings(settings) -> str | None:
    """Apply the saved policy and wake waiters; never manufacture a processing pool."""
    from ...jobs.group_runtime import refresh_worker_groups, wake_group_runtime
    from ._helpers import _ensure_gpu_cache
    from .api_jobs import _get_shared_worker_pool
    from .job_runner import _build_selected_gpus

    error = None
    try:
        pool = _get_shared_worker_pool()
        if pool is not None:
            detected = _ensure_gpu_cache()
            with settings.locked():
                refresh_worker_groups(pool, selected_gpus=_build_selected_gpus(settings, detected=detected), force=True)
        from .api_settings import _resize_text_detection_cpu_helpers

        _resize_text_detection_cpu_helpers()
    except Exception:
        logger.exception("Worker groups saved, but live reconciliation failed")
        error = "Worker groups were saved, but the live worker refresh failed. Check the application log."
    finally:
        wake_group_runtime()
    return error


def worker_group_payload(settings=None) -> dict:
    """Read current policy and activity without exposing private configuration."""
    from ._helpers import _ensure_gpu_cache
    from .api_jobs import _get_shared_worker_pool

    settings = settings or get_settings_manager()
    with settings.locked():
        groups = settings.worker_groups
        revision = settings.worker_groups_revision
    hardware = [
        {key: entry.get(key) for key in ("device", "name", "type", "status")}
        for entry in _ensure_gpu_cache()
        if isinstance(entry, dict)
    ]
    devices = {entry["device"] for entry in hardware if entry.get("status") != "failed"}
    pool = _get_shared_worker_pool()
    live = pool.group_snapshots() if pool is not None and hasattr(pool, "group_snapshots") else []
    live_by_id = {row["id"]: row for row in live}
    rows = []
    warnings = []
    for group in groups:
        row = live_by_id.pop(group["id"], None)
        detected = group["resource"] == "cpu" or group["device"] in devices
        opened = group_is_available(group)
        state = (
            "disabled"
            if not group["enabled"]
            else "hardware_unavailable"
            if not detected
            else "active"
            if opened
            else "off_hours"
        )
        next_opening = next_group_opening(group) if detected and not opened else None
        fallback_target = group["count"] if detected and opened else 0
        status = row or {}
        rows.append(
            {
                "id": group["id"],
                "name": group["name"],
                "resource": group["resource"],
                "device": group["device"],
                "desired": group["count"] if group["enabled"] else 0,
                "target": status.get("target", fallback_target),
                "available": 0 if settings.processing_paused else status.get("available", fallback_target),
                "busy": max(0, status.get("running", 0) - status.get("finishing", 0)),
                "finishing": status.get("finishing", 0),
                "state": status.get("state", state),
                "next_available_at": status.get("next_opening") or (next_opening.isoformat() if next_opening else None),
            }
        )
        if group["enabled"] and not detected:
            warnings.append(
                {
                    "code": "hardware_unavailable",
                    "group_id": group["id"],
                    "message": f"{group['name']}: GPU unavailable. Check its device.",
                }
            )
    for row in live_by_id.values():
        if row.get("finishing") or row.get("running"):
            rows.append(
                {
                    **row,
                    "desired": 0,
                    "available": 0,
                    "busy": 0,
                    "finishing": max(row.get("finishing", 0), row.get("running", 0)),
                    "state": "draining",
                    "next_available_at": None,
                }
            )
    enabled_kinds = {JOB_KIND_PREVIEWS}
    for server in settings.get("media_servers", []) or []:
        if not isinstance(server, dict) or not server.get("enabled", True):
            continue
        if (server.get("loudness") or {}).get("enabled"):
            enabled_kinds.add(JOB_KIND_LOUDNESS)
        if (server.get("markers") or {}).get("enabled"):
            enabled_kinds.add(JOB_KIND_INTRO_CREDITS)
    labels = {
        JOB_KIND_PREVIEWS: "Video previews",
        JOB_KIND_INTRO_CREDITS: "Intro & Credits",
        JOB_KIND_LOUDNESS: "Plex loudness",
    }
    for kind in sorted(enabled_kinds):
        if not future_job_capacity(settings, kind):
            warnings.append(
                {
                    "code": "no_eligible_workers",
                    "job_type": kind,
                    "message": f"{labels[kind]} has no compatible worker hours outside global quiet hours. Add or enable a group and check its hours.",
                }
            )
    gpu_peak, cpu_peak = configured_group_totals(groups)
    timezone = application_timezone()
    timezone_name = str(timezone)
    timezone_label = timezone_name
    if timezone_name == "Local time":
        offset = datetime.now(timezone).strftime("%z")
        timezone_label = f"Local time (UTC{offset[:3]}:{offset[3:]})"
    return {
        "groups": groups,
        "revision": revision,
        "timezone": timezone_name,
        "timezone_label": timezone_label,
        "limits": {"cpu": MAX_CPU_WORKERS, "gpu": MAX_GPU_WORKERS},
        "hardware": hardware,
        "capacity": {
            "groups": rows,
            "current": {
                resource: sum(row.get("target", 0) for row in rows if row.get("resource") == resource)
                for resource in ("cpu", "gpu")
            },
            "peak": {"cpu": cpu_peak, "gpu": gpu_peak},
        },
        "warnings": warnings,
        "processing_paused": settings.processing_paused,
        "pause_reasons": settings.processing_pause_reasons,
    }


@api.route("/worker-groups", methods=["GET"])
@setup_or_auth_required
def get_worker_groups():
    """Return worker configuration and the current reasons for waiting."""
    return jsonify(worker_group_payload())


@api.route("/worker-groups", methods=["PUT"])
@setup_or_auth_required
def save_worker_groups():
    """Commit a staged policy, refusing to overwrite concurrent quick scaling."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("revision")) is not int or "groups" not in data:
        return jsonify({"error": "Provide groups and the revision you loaded"}), 400
    settings = get_settings_manager()
    try:
        settings.update_worker_groups(data["groups"], expected_revision=data["revision"])
    except WorkerGroupsConflict as exc:
        return jsonify({"error": str(exc), "revision": settings.worker_groups_revision}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    error = reconcile_group_settings(settings)
    result = worker_group_payload(settings)
    result["success"] = True
    if error:
        result["warning"] = error
    return jsonify(result)


def scale_saved_group(group_id: str, *, delta: int | None = None, enabled: bool | None = None) -> None:
    """Change one saved group under the same lock and validation as a full edit."""
    settings = get_settings_manager()
    with settings.locked():
        groups = settings.worker_groups
        group = next((entry for entry in groups if entry["id"] == group_id), None)
        if group is None:
            raise KeyError(group_id)
        if enabled is not None:
            group["enabled"] = enabled
        elif delta is not None:
            target = (group["count"] if group["enabled"] else 0) + delta
            if target <= 0:
                group["enabled"] = False
            else:
                group["count"] = target
                group["enabled"] = True
        settings.update_worker_groups(groups)


@api.route("/worker-groups/<group_id>/scale", methods=["POST"])
@setup_or_auth_required
def scale_worker_group(group_id: str):
    """Apply a quick count/enable change without a stale whole-policy payload."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Provide a scaling action"}), 400
    if set(data) == {"delta"} and type(data["delta"]) is int and data["delta"] in (-1, 1):
        changes = {"delta": data["delta"]}
    elif set(data) == {"enabled"} and isinstance(data["enabled"], bool):
        changes = {"enabled": data["enabled"]}
    else:
        return jsonify({"error": "Provide delta +1/-1 or enabled true/false"}), 400
    try:
        scale_saved_group(group_id, **changes)
    except KeyError:
        return jsonify({"error": "Worker group no longer exists"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    settings = get_settings_manager()
    error = reconcile_group_settings(settings)
    result = worker_group_payload(settings)
    result["success"] = True
    if error:
        result["warning"] = error
    return jsonify(result)
