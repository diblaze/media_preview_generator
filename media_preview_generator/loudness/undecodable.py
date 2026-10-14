"""Audio streams the ffmpeg in use has no decoder for (AV3A "Audio Vivid" is the example), remembered so loudness
stops retrying them.

The key is Plex's part hash (content-based, so a replaced file gets a new hash), the stream index, the codec and the
ffmpeg that failed, so an upgraded or different ffmpeg tries the stream again. Reading never fails a job or a Library
Health count: an unreadable store reads as empty.
"""

from __future__ import annotations

import functools
import os
import sqlite3
import subprocess
import threading
from contextlib import closing
from datetime import UTC, datetime

from loguru import logger

from .plex_db import AudioStream

_TIMEOUT_S = 30
_lock = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS undecodable_streams (
    part_hash TEXT NOT NULL,
    stream_index INTEGER NOT NULL,
    codec TEXT NOT NULL,
    ffmpeg TEXT NOT NULL,
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (part_hash, stream_index, codec, ffmpeg)
)
"""


def db_path() -> str:
    """Where the store lives: ``<CONFIG_DIR>/loudness.db``."""
    return os.path.join(os.environ.get("CONFIG_DIR", "/config"), "loudness.db")


@functools.cache
def ffmpeg_identity(binary: str) -> str:
    """Which ffmpeg this is: its real path and the first line of ``-version``; just the path if it won't say."""
    real = os.path.realpath(binary)
    try:
        out = subprocess.run([binary, "-version"], capture_output=True, text=True, timeout=20, check=True).stdout
    except Exception as exc:  # any failure to read the version leaves the path as the identity
        logger.debug("Couldn't read {} -version: {}", binary, exc)
        return real
    lines = out.splitlines()
    return f"{real} {lines[0]}" if lines else real


def record(
    part_hash: str | None, stream_index: int, codec: str, reason: str, ffmpeg: str, path: str | None = None
) -> bool:
    """Remember that an ffmpeg can't decode a stream.

    Args:
        part_hash: Plex's hash of the part.
        stream_index: The stream's index in the file.
        codec: The stream's codec.
        reason: Why it can't be measured.
        ffmpeg: The failing ffmpeg's ``ffmpeg_identity``.
        path: The store file; defaults to ``db_path()``.

    Returns:
        True when stored; False for a part without a hash or when the store can't be written.
    """
    if not part_hash:
        return False
    try:
        with _lock, closing(sqlite3.connect(path or db_path(), timeout=_TIMEOUT_S)) as conn:
            conn.execute(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO undecodable_streams VALUES (?, ?, ?, ?, ?, ?)",
                (part_hash, stream_index, codec, ffmpeg, reason, datetime.now(UTC).isoformat()),
            )
            conn.commit()
    except (sqlite3.Error, OSError) as exc:
        logger.warning("Couldn't remember that {} can't decode stream {} ({}): {}", ffmpeg, stream_index, codec, exc)
        return False
    return True


def _read(query: str, ffmpeg: str, path: str | None) -> list[tuple] | None:
    """The query's rows, or None when the store can't be read."""
    try:
        with _lock, closing(sqlite3.connect(path or db_path(), timeout=_TIMEOUT_S)) as conn:
            conn.execute(_SCHEMA)
            return conn.execute(query, (ffmpeg,)).fetchall()
    except (sqlite3.Error, OSError) as exc:
        logger.debug("Loudness undecodable-stream store unreadable, treating it as empty: {}", exc)
        return None


def read_skipped_keys(ffmpeg: str, path: str | None = None) -> frozenset[tuple[str, int, str]] | None:
    """Like ``skipped_keys``, but None when the store can't be read, so a caller can ask again later."""
    rows = _read("SELECT part_hash, stream_index, codec FROM undecodable_streams WHERE ffmpeg = ?", ffmpeg, path)
    return None if rows is None else frozenset((h, i, c) for h, i, c in rows)


def skipped_keys(ffmpeg: str, path: str | None = None) -> frozenset[tuple[str, int, str]]:
    """Every ``(part_hash, stream_index, codec)`` this ffmpeg failed on."""
    return read_skipped_keys(ffmpeg, path) or frozenset()


def skipped_count_by_part_hash(ffmpeg: str, path: str | None = None) -> dict[str, int]:
    """How many streams this ffmpeg failed on, per part hash."""
    query = "SELECT part_hash, COUNT(*) FROM undecodable_streams WHERE ffmpeg = ? GROUP BY part_hash"
    return dict(_read(query, ffmpeg, path) or [])


def is_skipped(stream: AudioStream, keys: frozenset[tuple[str, int, str]]) -> bool:
    """Whether a Plex audio stream is recorded as undecodable."""
    return stream.part_hash is not None and (stream.part_hash, stream.index, stream.codec) in keys
