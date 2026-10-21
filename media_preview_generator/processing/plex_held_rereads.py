"""Plex re-read requests held for Plex's maintenance window, kept in a small sqlite file so a restart keeps them."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from loguru import logger

from ..servers.plex_scheduled_jobs import read_settings, should_hold

if TYPE_CHECKING:
    from ..servers.plex import PlexServer

_DB_NAME = "plex_held_rereads.db"
_lock = threading.Lock()
_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS held ("
    "server_id TEXT NOT NULL, canonical_path TEXT NOT NULL, payload TEXT NOT NULL, held_at REAL NOT NULL, "
    "PRIMARY KEY (server_id, canonical_path))"
)
# Held requests a flush leaves unsent so the notification queue keeps room for jobs that are publishing.
_QUEUE_HEADROOM = 32


@dataclass(frozen=True)
class HeldReread:
    """One re-read request waiting for its server's maintenance window."""

    server_id: str
    canonical_path: str
    item_id: str | None
    library_id: str | None
    held_at: float


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """A connection to the held list in CONFIG_DIR, committed and closed on exit."""
    conn = sqlite3.connect(Path(os.environ.get("CONFIG_DIR", "/config")) / _DB_NAME, timeout=10)
    try:
        with conn:
            conn.execute(_SCHEMA)
            yield conn
    finally:
        conn.close()


def _from_row(server_id: str, canonical_path: str, payload: str, held_at: float) -> HeldReread:
    data = json.loads(payload)
    return HeldReread(
        server_id=server_id,
        canonical_path=canonical_path,
        item_id=data.get("item_id"),
        library_id=data.get("library_id"),
        held_at=held_at,
    )


def hold(server_id: str, canonical_path: str, *, item_id: str | None, library_id: str | None) -> None:
    """Remember a request to re-read ``canonical_path`` later, replacing any earlier one for it."""
    payload = json.dumps({"item_id": item_id, "library_id": library_id})
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO held (server_id, canonical_path, payload, held_at) VALUES (?, ?, ?, ?)",
            (server_id, canonical_path, payload, time.time()),
        )


def release(server_id: str, canonical_path: str) -> None:
    """Forget a held request; does nothing when there is none."""
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM held WHERE server_id = ? AND canonical_path = ?", (server_id, canonical_path))


def entries(server_id: str | None = None) -> list[HeldReread]:
    """Held requests, for one server or all of them; an unreadable list counts as empty."""
    query = "SELECT server_id, canonical_path, payload, held_at FROM held"
    args: tuple[str, ...] = ()
    if server_id is not None:
        query += " WHERE server_id = ?"
        args = (server_id,)
    try:
        with _lock, _connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_from_row(*row) for row in rows]
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.warning(
            "Could not read {}: {}. Held Plex re-reads are forgotten; the next job asks again.", _DB_NAME, exc
        )
        return []


def is_held(server_id: str, canonical_path: str) -> bool:
    """Whether a request for this file waits for its server's maintenance window; an unreadable list says no."""
    try:
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM held WHERE server_id = ? AND canonical_path = ?", (server_id, canonical_path)
            ).fetchone()
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Could not read {}: {}", _DB_NAME, exc)
        return False
    return row is not None


def held_paths(server_id: str) -> set[str]:
    """The canonical paths with a held request on this server."""
    return {e.canonical_path for e in entries(server_id)}


def flush_held_rereads() -> None:
    """Send the held requests of every server whose maintenance window is open (or whose hold no longer applies).

    Runs on the scheduler. Requests for servers that are gone or disabled are dropped. At most as many requests
    as the notification queue has room for (minus some headroom) are sent, and files already queued are skipped;
    the rest wait for the next run. It also re-reads every Plex server's job settings, so the job log can say a
    preview is held without contacting Plex while publishing.
    """
    from ..servers import ServerRegistry, ServerType
    from ..web.settings_manager import get_settings_manager
    from .plex_refresh import enqueue_plex_refresh, free_plex_refresh_slots, is_plex_refresh_queued

    registry = ServerRegistry.from_settings(list(get_settings_manager().get("media_servers") or []))
    servers: dict[str, PlexServer] = {}
    for config in registry.configs():
        server = registry.get(config.id)
        if config.enabled and config.type is ServerType.PLEX and server is not None:
            servers[config.id] = cast("PlexServer", server)
            read_settings(servers[config.id], fresh=True)
    held = entries()
    if not held:
        return
    slots = free_plex_refresh_slots() - _QUEUE_HEADROOM
    ready: dict[str, list[HeldReread]] = {}
    for entry in held:
        server = servers.get(entry.server_id)
        if server is None:
            release(entry.server_id, entry.canonical_path)
        elif not should_hold(server, entry.library_id) and not is_plex_refresh_queued(
            entry.server_id, entry.canonical_path
        ):
            ready.setdefault(entry.server_id, []).append(entry)
    for server_id, due in ready.items():
        server = servers[server_id]
        due = due[: max(slots, 0)]
        if not due:
            continue
        logger.info(
            "Asking {} to show {} held preview(s) now that its maintenance window is open", server.name, len(due)
        )
        for entry in due:
            enqueue_plex_refresh(
                server,
                entry.canonical_path,
                entry.item_id,
                library_id=entry.library_id,
            )
        slots -= len(due)
