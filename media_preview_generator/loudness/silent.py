"""Audio streams this app measured as silent (``-inf`` loudness) over the whole stream, remembered so a stream stored as
``-inf`` by an older release is measured again once and a really silent one isn't measured on every scan.

Releases before ``-reinit_filter 0`` stored ``-inf`` for a track whose audio changed near its end: loudnorm restarted
there and reported only the tail. Plex's own analysis restarts the same way and its ``-inf`` looks the same, so that is
measured again too; the write is in the undo log like any other. The key is Plex's part hash (content-based, so a
replaced file gets a new hash), the stream index and the codec, not the ffmpeg: a whole-stream measurement of silence
stays silence. Reading never fails a job: an unreadable store reads as empty, so the stream is measured again.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime

from loguru import logger

from .undecodable import db_path

_TIMEOUT_S = 30
_lock = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS silent_streams (
    part_hash TEXT NOT NULL,
    stream_index INTEGER NOT NULL,
    codec TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (part_hash, stream_index, codec)
)
"""


def record(part_hash: str | None, stream_index: int, codec: str, path: str | None = None) -> bool:
    """Remember that a stream measured silent over its whole length.

    Args:
        part_hash: Plex's hash of the part.
        stream_index: The stream's index in the file.
        codec: The stream's codec.
        path: The store file; defaults to ``undecodable.db_path()``.

    Returns:
        True when stored; False for a part without a hash or when the store can't be written.
    """
    if not part_hash:
        return False
    try:
        with _lock, closing(sqlite3.connect(path or db_path(), timeout=_TIMEOUT_S)) as conn:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO silent_streams VALUES (?, ?, ?, ?)",
                (part_hash, stream_index, codec, datetime.now(UTC).isoformat()),
            )
            conn.commit()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Couldn't remember that stream {} ({}) is silent: {}", stream_index, codec, exc)
        return False
    return True


def read_keys(path: str | None = None) -> frozenset[tuple[str, int, str]] | None:
    """Every ``(part_hash, stream_index, codec)`` measured silent, or None when the store can't be read."""
    try:
        with _lock, closing(sqlite3.connect(path or db_path(), timeout=_TIMEOUT_S)) as conn:
            conn.execute(_SCHEMA)
            rows = conn.execute("SELECT part_hash, stream_index, codec FROM silent_streams").fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Loudness silent-stream store unreadable, treating it as empty: {}", exc)
        return None
    return frozenset((h, i, c) for h, i, c in rows)
