"""Validated worker policy and weekly availability, independent of live workers."""

from __future__ import annotations

import copy
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .job_kinds import JOB_KIND_LOUDNESS, JOB_KINDS

MAX_CPU_WORKERS = 32
MAX_GPU_WORKERS = 32
MAX_GROUPS = 64
WEEK_MINUTES = 7 * 24 * 60
_ALL_WEEK = (1 << WEEK_MINUTES) - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def application_timezone() -> tzinfo:
    """Return the application's local timezone with its daylight-saving rules."""
    return _timezone_for_name(os.environ.get("TZ", "").lstrip(":"))


@lru_cache(maxsize=8)
def _timezone_for_name(name: str) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    try:
        resolved = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in resolved:
            return ZoneInfo(resolved.split("/zoneinfo/", 1)[1])
        with open("/etc/localtime", "rb") as stream:
            return ZoneInfo.from_file(stream, key="Local time")
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return UTC


def local_now(now: datetime | None = None) -> datetime:
    """Use application time, or an explicitly supplied local clock for evaluation."""
    if now is None:
        return datetime.now(application_timezone())
    return now if now.tzinfo is not None else now.replace(tzinfo=application_timezone())


def group_resource_key(group: dict) -> str:
    """Identify the shared capacity budget, separately from individual groups."""
    return "cpu" if group["resource"] == "cpu" else f"gpu:{group['device']}"


def supports_job(group: dict, kind: str) -> bool:
    """Apply intrinsic hardware capability before the owner's job permissions."""
    return (
        kind in JOB_KINDS
        and kind in group.get("job_types", [])
        and (kind != JOB_KIND_LOUDNESS or group.get("resource") == "cpu")
    )


def _minute(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def group_weekly_mask(group: dict) -> int:
    """Return eligible wall-clock minutes; overlapping windows count once."""
    if not group.get("enabled", True):
        return 0
    availability = group.get("availability", {"mode": "always"})
    if availability.get("mode") == "always":
        return _ALL_WEEK
    mask = 0
    for window in availability.get("windows", []):
        start, end = _minute(window["start"]), _minute(window["end"])
        duration = (end - start) % (24 * 60)
        for day in window["days"]:
            offset = day * 24 * 60 + start
            segment = ((1 << duration) - 1) << offset
            mask |= (segment & _ALL_WEEK) | (segment >> WEEK_MINUTES)
    return mask


def _clock_minute(now: datetime) -> int:
    return now.weekday() * 1440 + now.hour * 60 + now.minute


def group_is_available(group: dict, now: datetime | None = None) -> bool:
    """Whether this group can start new files at the supplied local wall time."""
    if not group.get("enabled", True):
        return False
    if group.get("availability", {}).get("mode", "always") == "always":
        return True
    return bool(group_weekly_mask(group) & (1 << _clock_minute(local_now(now))))


def next_mask_opening(mask: int, now: datetime | None = None) -> datetime | None:
    """Find real elapsed-time availability, including skipped/repeated DST hours."""
    if not mask:
        return None
    current = local_now(now)
    if mask & (1 << _clock_minute(current)):
        return current
    # Walk elapsed minutes, not local arithmetic: a skipped 02:30 must not become
    # an imaginary time, and both occurrences of a repeated hour are eligible.
    cursor = current.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # A weekly window can disappear entirely in a spring-forward gap. Search
    # through the following week too, rather than reporting no future opening.
    for _ in range(15 * 1440):
        candidate = cursor.astimezone(current.tzinfo)
        if mask & (1 << _clock_minute(candidate)):
            return candidate
        cursor += timedelta(minutes=1)
    return None


def next_group_opening(group: dict, now: datetime | None = None) -> datetime | None:
    """Return the next eligible instant, or None for a disabled group."""
    return next_mask_opening(group_weekly_mask(group), now)


def _peak(groups: list[dict]) -> int:
    changes: dict[int, int] = {}
    for group in groups:
        mask = group_weekly_mask(group)
        while mask:
            start = (mask & -mask).bit_length() - 1
            shifted = mask >> start
            length = (shifted ^ (shifted + 1)).bit_length() - 1
            end = start + length
            changes[start] = changes.get(start, 0) + group["count"]
            changes[end] = changes.get(end, 0) - group["count"]
            mask &= ~(((1 << length) - 1) << start)
    current = peak = 0
    for point in sorted(changes):
        current += changes[point]
        peak = max(peak, current)
    return peak


def configured_group_totals(groups: list[dict]) -> tuple[int, int]:
    """Return peak (GPU, CPU) capacity, retaining the existing family limits."""
    return tuple(_peak([g for g in groups if g["resource"] == resource]) for resource in ("gpu", "cpu"))


def validate_worker_groups(value: object) -> list[dict[str, Any]]:
    """Normalize worker groups, rejecting malformed or overcommitted policies.

    Args:
        value: Complete proposed group list; an empty list intentionally disables capacity.

    Returns:
        An independent normalized list safe to persist.

    Raises:
        ValueError: A field is invalid, or overlapping groups exceed capacity limits.
    """
    if not isinstance(value, list) or len(value) > MAX_GROUPS:
        raise ValueError(f"Worker groups must be a list of at most {MAX_GROUPS} groups")
    groups: list[dict] = []
    ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Each worker group must be an object")
        group_id = raw.get("id")
        if not isinstance(group_id, str) or not _ID.fullmatch(group_id) or group_id in ids:
            raise ValueError("Each worker group needs a unique valid ID")
        ids.add(group_id)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Group names must contain between 1 and 80 characters")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{name}: enabled must be true or false")
        resource = raw.get("resource")
        if resource not in ("cpu", "gpu"):
            raise ValueError(f"{name}: choose CPU or GPU")
        device = raw.get("device")
        if resource == "gpu" and (not isinstance(device, str) or not device.strip() or len(device) > 256):
            raise ValueError(f"{name}: choose a GPU device")
        if resource == "cpu" and device not in (None, ""):
            raise ValueError(f"{name}: a CPU group cannot select a GPU device")
        count = raw.get("count")
        limit = MAX_CPU_WORKERS if resource == "cpu" else MAX_GPU_WORKERS
        if type(count) is not int or not 1 <= count <= limit:
            raise ValueError(f"{name}: worker count must be between 1 and {limit}; disable the group to use zero")
        kinds = raw.get("job_types")
        if not isinstance(kinds, list) or not kinds or any(k not in JOB_KINDS for k in kinds):
            raise ValueError(f"{name}: select at least one supported job type")
        if resource == "gpu" and JOB_KIND_LOUDNESS in kinds:
            raise ValueError(f"{name}: Plex loudness requires CPU workers")
        availability = raw.get("availability", {"mode": "always", "windows": []})
        if not isinstance(availability, dict) or availability.get("mode") not in ("always", "scheduled"):
            raise ValueError(f"{name}: availability must be always or scheduled")
        windows = availability.get("windows", [])
        if not isinstance(windows, list) or len(windows) > 32:
            raise ValueError(f"{name}: use at most 32 availability windows")
        if availability["mode"] == "scheduled" and not windows:
            raise ValueError(f"{name}: add an availability window")
        clean_windows = []
        for window in windows:
            if not isinstance(window, dict):
                raise ValueError(f"{name}: each window must be an object")
            days = window.get("days")
            if not isinstance(days, list) or not days or any(type(d) is not int or not 0 <= d <= 6 for d in days):
                raise ValueError(f"{name}: select valid start days for each window")
            start, end = window.get("start"), window.get("end")
            if any(not isinstance(t, str) or not _TIME.fullmatch(t) for t in (start, end)) or start == end:
                raise ValueError(f"{name}: use different start and end times in HH:MM format")
            clean_windows.append({"days": sorted(set(days)), "start": start, "end": end})
        groups.append(
            {
                "id": group_id,
                "name": name.strip(),
                "enabled": enabled,
                "resource": resource,
                "device": device if resource == "gpu" else None,
                "count": count,
                "job_types": [kind for kind in JOB_KINDS if kind in kinds],
                "availability": {"mode": availability["mode"], "windows": clean_windows},
            }
        )
    gpu, cpu = configured_group_totals(groups)
    if cpu > MAX_CPU_WORKERS or gpu > MAX_GPU_WORKERS:
        raise ValueError(
            f"Overlapping groups exceed capacity: peak CPU {cpu}/{MAX_CPU_WORKERS}, GPU {gpu}/{MAX_GPU_WORKERS}. "
            "Reduce counts or use different hours."
        )
    return groups


def groups_from_legacy(settings: dict) -> list[dict]:
    """Preserve explicitly configured allocations; zero CPU remains zero."""
    groups = []

    def append(group_id: str, name: str, resource: str, count: int, device: str | None, enabled: bool) -> None:
        groups.append(
            {
                "id": group_id,
                "name": name,
                "enabled": enabled and count > 0,
                "resource": resource,
                "device": device,
                "count": max(1, count),
                "job_types": [kind for kind in JOB_KINDS if resource == "cpu" or kind != JOB_KIND_LOUDNESS],
                "availability": {"mode": "always", "windows": []},
            }
        )

    cpu = settings.get("cpu_threads", 1)
    cpu = int(cpu) if cpu not in (None, "") else 1
    if cpu > 0:
        append("legacy-cpu", "CPU workers", "cpu", cpu, None, True)
    for entry in settings.get("gpu_config") or []:
        if not isinstance(entry, dict) or not entry.get("device"):
            continue
        device = str(entry["device"])
        group_id = "legacy-gpu-" + hashlib.sha256(device.encode()).hexdigest()[:16]
        append(
            group_id,
            str(entry.get("name") or device)[:80],
            "gpu",
            int(entry.get("workers", 1)),
            device,
            bool(entry.get("enabled", True)),
        )
    return validate_worker_groups(groups)


def effective_worker_groups(settings: dict) -> list[dict]:
    """Use groups when present, including [], and legacy counts only when absent."""
    if "worker_groups" in settings:
        return validate_worker_groups(settings["worker_groups"])
    return groups_from_legacy(copy.deepcopy(settings))


def future_capacity(groups: list[dict], quiet_hours: object, kind: str) -> int:
    """Count configured capacity with reachable hours outside the global pause rule."""
    from .quiet_hours import quiet_hours_weekly_mask

    blocked = quiet_hours_weekly_mask(quiet_hours)
    return sum(group["count"] for group in groups if supports_job(group, kind) and group_weekly_mask(group) & ~blocked)
