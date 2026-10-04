"""Per-server loudness settings (``media_servers[].loudness``): a switch and the libraries it goes to."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..servers.base import Library, ServerConfig

# Plex uses "episode" for TV sections; older saved configs can call them "show".
# Music also needs album gain and fades, which this video feature does not generate.
DEFAULT_KINDS = frozenset({"movie", "episode", "show"})
NO_AGENT = (
    "Loudness analysis requires a local Plex database on the same machine as this app. "
    "The Plex helper does not support loudness analysis."
)
# Server ids already warned about an invalid stored block: it's read per file, so it's said once per process.
_warned: set[str] = set()


def default_server_loudness() -> dict[str, Any]:
    """The per-server block before the user turns loudness on: off, default libraries."""
    return {"enabled": False, "library_ids": None}


@dataclass(frozen=True)
class ServerLoudnessSettings:
    """Typed view of ``media_servers[].loudness``."""

    enabled: bool
    library_ids: tuple[str, ...] | None


def _plex_block(markers: object) -> dict:
    plex = markers.get("plex") if isinstance(markers, dict) else None
    return plex if isinstance(plex, dict) else {}


def _uses_agent(markers: object) -> bool:
    agent = _plex_block(markers).get("agent")
    return bool(isinstance(agent, dict) and agent.get("enabled"))


def validate_server_loudness(
    raw: object, server_type: str, markers: object, *, library_kinds: dict[str, str | None] | None = None
) -> tuple[dict | None, str]:
    """Validate and normalise a per-server ``loudness`` block.

    Args:
        raw: The posted block (None → defaults).
        server_type: ``plex``, ``emby`` or ``jellyfin``.
        markers: The server's ``markers`` block, which holds the shared Plex helper configuration.
        library_kinds: Known library types, so explicit non-video selections can be refused.

    Returns:
        ``(block, "")`` on success, ``(None, message)`` on error.
    """
    if raw is None:
        return default_server_loudness(), ""
    if not isinstance(raw, dict):
        return None, "loudness must be an object"
    library_ids_raw = raw.get("library_ids")
    library_ids: list[str] | None = None
    if library_ids_raw is not None:
        if not isinstance(library_ids_raw, list):
            return None, "loudness.library_ids must be a list or null"
        library_ids = list(dict.fromkeys(str(x) for x in library_ids_raw))
    enabled = raw.get("enabled", False)
    if type(enabled) is not bool:
        return None, "loudness.enabled must be a boolean"
    if enabled and server_type != "plex":
        return None, "Loudness analysis is for Plex servers only"
    if enabled and _uses_agent(markers):
        return None, NO_AGENT
    if (
        enabled
        and library_ids
        and any(
            (library_kinds or {}).get(lid) and (library_kinds or {})[lid] not in DEFAULT_KINDS for lid in library_ids
        )
    ):
        return (
            None,
            "Loudness analysis supports movie and TV libraries only; music and other library types are unsupported",
        )
    return {"enabled": enabled, "library_ids": library_ids}, ""


def load_server_loudness(cfg: ServerConfig) -> ServerLoudnessSettings:
    """A server's loudness settings, falling back to off when the stored block is invalid (logged)."""
    block, err = validate_server_loudness(
        cfg.loudness or None, cfg.type.value, cfg.markers, library_kinds={lib.id: lib.kind for lib in cfg.libraries}
    )
    if err or block is None:
        if cfg.id not in _warned:
            _warned.add(cfg.id)
            logger.warning(
                "Ignoring invalid loudness settings for server {!r} ({}); loudness is off there", cfg.name, err
            )
        block = default_server_loudness()
    ids = block["library_ids"]
    return ServerLoudnessSettings(enabled=block["enabled"], library_ids=tuple(ids) if ids is not None else None)


def library_chosen(settings: ServerLoudnessSettings, *, library_id: str | None, kind: str | None) -> bool:
    """Whether loudness goes to a library: the explicit choice when there is one, else movie and TV libraries."""
    if kind and kind not in DEFAULT_KINDS:
        return False
    if settings.library_ids is not None:
        return library_id is not None and library_id in settings.library_ids
    return kind in DEFAULT_KINDS


def loudness_libraries(cfg: ServerConfig) -> list[Library]:
    """The libraries loudness goes to on a server; empty when it's off there."""
    settings = load_server_loudness(cfg)
    if not settings.enabled:
        return []
    return [lib for lib in cfg.libraries if library_chosen(settings, library_id=lib.id, kind=lib.kind)]


def loudness_enabled_anywhere(configs: list[ServerConfig]) -> bool:
    """Whether any enabled server has loudness on."""
    return any(cfg.enabled and load_server_loudness(cfg).enabled for cfg in configs)
