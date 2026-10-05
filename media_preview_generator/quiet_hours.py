"""Quiet-hours migration and wall-clock policy without scheduler side effects."""

from __future__ import annotations

from .worker_groups import group_weekly_mask

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _time(value: object) -> str | None:
    try:
        hour, minute = str(value).split(":")
        h, m = int(hour), int(minute)
        return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else None
    except (ValueError, TypeError):
        return None


def migrate_quiet_hours(raw: dict | None) -> dict:
    """Preserve legacy effective weekly intervals while adopting start-day overnight windows.

    Legacy days select both the morning and evening of that calendar day. Thus
    Monday 23–07 becomes Monday 00–07 plus Monday 23–Tuesday 00. Empty/equal
    windows remain inactive. This conversion is idempotent via ``day_basis``.
    """
    raw = raw if isinstance(raw, dict) else {}
    source = raw.get("windows")
    if not isinstance(source, list) or (not source and raw.get("day_basis") != "start"):
        source = [{"start": raw.get("start"), "end": raw.get("end")}]
    windows = []
    for window in source:
        if not isinstance(window, dict):
            continue
        start, end = _time(window.get("start")), _time(window.get("end"))
        if start is None or end is None or start == end:
            continue
        raw_days = window.get("days")
        chosen = {str(day).strip().lower() for day in raw_days} if isinstance(raw_days, list) else set()
        days = [day for day in DAYS if day in chosen] or list(DAYS)
        if raw.get("day_basis") != "start" and start > end:
            if end != "00:00":
                windows.append({"start": "00:00", "end": end, "days": days})
            windows.append({"start": start, "end": "00:00", "days": days})
        else:
            windows.append({"start": start, "end": end, "days": days})
    return {"enabled": bool(raw.get("enabled")), "day_basis": "start", "windows": windows}


def quiet_hours_weekly_mask(raw: dict | None) -> int:
    """Return the union of paused wall-clock minutes, preserving old on-disk policy."""
    quiet = migrate_quiet_hours(raw)
    return group_weekly_mask(
        {
            "enabled": quiet["enabled"],
            "availability": {
                "mode": "scheduled",
                "windows": [
                    {**window, "days": [DAYS.index(day) for day in window["days"]]} for window in quiet["windows"]
                ],
            },
        }
    )
