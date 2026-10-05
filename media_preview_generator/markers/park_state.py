"""Completed marker bookkeeping retained when unfinished work leaves memory.

Call only after the dispatcher has fenced and drained every callback. Never
serialize live clients, capability caches, file locks or detector objects.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

from .job_log import SeasonEpisode
from .models import Source


def snapshot_context(ctx) -> dict:
    """Capture settled counts and obligations; unfinished files are freshly checked."""
    return {
        "decided_by": ctx.decided_by.snapshot(),
        "followups": sorted(ctx._followups),
        "left_out_changed": sorted(ctx._left_out_changed),
        "budget_rechecks": sorted(ctx._budget_rechecks),
        "budget_refused_at": ctx._budget_refused_at.isoformat() if ctx._budget_refused_at else None,
        "budget_exhausted": {key.value: value for key, value in ctx._budget_exhausted.items()},
        "key_refused": {key.value: value for key, value in ctx._key_refused.items()},
        "sent": dict(ctx._sent),
        "seasons": {key: [asdict(value) for value in values] for key, values in ctx._seasons.items()},
        "decided_again": ctx._decided_again,
        "rechecked_online": ctx._rechecked_online,
        "answers_changed": ctx._answers_changed,
        "missing": ctx._missing,
        "busy_promised": sorted(ctx._busy_promised),
        "replaced_at_start": ctx._replaced_at_start,
    }


def restore_context(ctx, saved: dict) -> None:
    """Seed a fresh context before submission, preserving promised follow-up work."""
    ctx.decided_by.restore(saved.get("decided_by", {}))
    for name in ("followups", "left_out_changed", "budget_rechecks", "busy_promised"):
        setattr(ctx, "_" + name, set(saved.get(name, [])))
    raw = saved.get("budget_refused_at")
    ctx._budget_refused_at = datetime.fromisoformat(raw) if raw else None
    ctx._budget_exhausted = {Source(key): value for key, value in saved.get("budget_exhausted", {}).items()}
    ctx._key_refused = {Source(key): tuple(value) for key, value in saved.get("key_refused", {}).items()}
    ctx._sent = dict(saved.get("sent", {}))
    ctx._seasons = {
        key: [SeasonEpisode(**value) for value in values] for key, values in saved.get("seasons", {}).items()
    }
    ctx._decided_again = [tuple(row) for row in saved.get("decided_again", [])]
    ctx._rechecked_online = [tuple(row) for row in saved.get("rechecked_online", [])]
    ctx._answers_changed = bool(saved.get("answers_changed"))
    ctx._missing = int(saved.get("missing", 0))
    ctx._replaced_at_start = dict(saved.get("replaced_at_start", {}))
