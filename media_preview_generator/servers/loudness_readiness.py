"""Setup Health for the independently enabled local Plex loudness writer."""

from __future__ import annotations

from typing import Any

from loguru import logger

from .base import MediaServer, ServerConfig
from .chapter_readiness import NATIVE_MODES

# Plex's own server-wide "Analyze audio tracks for loudness": one setting for every library, music included.
PLEX_LOUDNESS_PREF = "LoudnessAnalysisBehavior"
_NEVER_ACTION = {
    "action": "set_plex_loudness_never",
    "args": {},
    "confirm": {
        "kind": "button",
        "phrase": "",
        "body": (
            "Sets Plex's server-wide <em>Analyze audio tracks for loudness</em> to Never (Plex Settings → Library). "
            "Plex stops analysing loudness itself in every library, music included. This app analyses only the movie "
            "and TV libraries you chose, so keep Plex's analysis on if you rely on loudness leveling or smart transitions "
            "for music."
        ),
    },
}


def loudness_readiness_section(
    server: MediaServer, server_config: ServerConfig | None, preferences: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Report whether the selected Plex database can receive loudness measurements.

    Args:
        server: Plex client whose identity must match the local database.
        server_config: Saved configuration, including the independent loudness opt-in.
        preferences: Plex's server prefs by id (``/:/prefs``); its own loudness analysis gets a row when known.

    Returns:
        A health section, or None when loudness is disabled.
    """
    if server_config is None:
        return None
    from ..loudness.guard import loudness_capability
    from ..loudness.settings import validate_server_loudness

    try:
        block, reason = validate_server_loudness(
            server_config.loudness,
            server_config.type.value,
            server_config.markers,
            library_kinds={lib.id: lib.kind for lib in server_config.libraries},
        )
        if not reason and not block["enabled"]:
            return None
        ready = False
        if not reason:
            report = loudness_capability(server, server_config)
            ready, reason = report.ready, report.message
    except Exception as exc:
        logger.debug("Loudness readiness probe failed for {} ({})", server.id, type(exc).__name__)
        ready = False
        reason = "Could not check loudness analysis. Check the Plex connection and try Setup Health again."
    checks = [
        {
            "id": "loudness_registration",
            "label": "Loudness measurements can be stored" if ready else "Loudness measurements cannot be stored",
            "docs_anchor": "plex-loudness",
            "tooltip": "Checks the local Plex database and server identity without changing them.",
            "explanation": (
                "<p>Loudness requires Plex and this app on the same machine, with Plex's config folder mounted "
                "locally. The database must match the connected server and supported Plex version.</p>"
                "<p>Intro &amp; Credits can stay off. The Plex helper does not support loudness analysis.</p>"
            ),
            "ok": ready,
            "severity": "recommended" if ready else "critical",
            "reason": None if ready else reason,
            "actions": {},
            "meta": {},
        }
    ]
    mode = (preferences or {}).get(PLEX_LOUDNESS_PREF)
    known = isinstance(mode, str) and mode in NATIVE_MODES
    plex_off = mode == "never"
    # Only beside a writer that can store: without it, Never would leave no loudness analysis at all.
    if known and ready:
        checks.append(
            {
                "id": "loudness_plex_analysis",
                "label": "Plex's own loudness analysis is off" if plex_off else "Plex also analyses loudness itself",
                "docs_anchor": "plex-loudness",
                "tooltip": "Plex's server-wide Analyze audio tracks for loudness setting, for every library, music too.",
                "explanation": (
                    "<p>Plex's <em>Analyze audio tracks for loudness</em> (Plex Settings → Library) is one setting for "
                    "every library, music included. While it's on, Plex analyses the same movie and TV tracks as this "
                    "app, one at a time, and can overlap a new file's webhook follow-up.</p>"
                    "<p>This app never analyses music: keep Plex's analysis on if you use loudness leveling or smart "
                    "transitions for music.</p>"
                ),
                "ok": plex_off,
                "severity": "recommended",
                "current": NATIVE_MODES[mode],
                "recommended": "Never",
                "reason": None
                if plex_off
                else "Plex analyses the same tracks one at a time. Set it to Never unless music libraries need it.",
                "actions": {} if plex_off else {"disable": _NEVER_ACTION},
                "fix_action": "disable",
                "fix_label": "Set to Never",
                # Server-wide, music included: never part of a bulk fix.
                "bulk": False,
                "meta": {"flag": PLEX_LOUDNESS_PREF},
            }
        )
    return {
        "id": "plex_loudness",
        "title": "Plex loudness",
        "docs_anchor": "plex-loudness",
        "ok": ready and (plex_off or not known),
        "severity": "recommended" if ready else "critical",
        "checks": checks,
    }
