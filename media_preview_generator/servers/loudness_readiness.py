"""Setup Health for the independently enabled local Plex loudness writer."""

from __future__ import annotations

from typing import Any

from loguru import logger

from .base import MediaServer, ServerConfig


def loudness_readiness_section(server: MediaServer, server_config: ServerConfig | None) -> dict[str, Any] | None:
    """Report whether the selected Plex database can receive loudness measurements.

    Args:
        server: Plex client whose identity must match the local database.
        server_config: Saved configuration, including the independent loudness opt-in.

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
    return {
        "id": "plex_loudness",
        "title": "Plex loudness",
        "docs_anchor": "plex-loudness",
        "ok": ready,
        "severity": "recommended" if ready else "critical",
        "checks": [
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
        ],
    }
