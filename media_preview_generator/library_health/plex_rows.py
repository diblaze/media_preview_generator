"""The fixed queries Library Health runs on Plex's database, and their rows in one JSON-safe shape.

The app runs them on a database it can open itself; a Plex database node (the Plex helper) runs the very same ones on
Plex's machine and sends the rows back. Nothing here takes SQL from a caller: the only inputs are section ids, bound
as parameters. Stdlib only, so the node's slim builds can import it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

# What a Plex database node advertises when it can run these queries.
LIBRARY_HEALTH_CAPABILITY = "library_health_v1"
# The most library sections one request may name: far more than any real Plex has.
MAX_SECTIONS = 1000

PARTS_SQL = """
SELECT mi.library_section_id, mi.id, mi.metadata_type, mp.id, mp.file, mp.hash, mp.extra_data,
       mi.title, parent.title AS season, grand.title AS show, mi."index", parent."index", mdi.id
FROM metadata_items mi
JOIN media_items mdi ON mdi.metadata_item_id = mi.id AND mdi.deleted_at IS NULL
JOIN media_parts mp ON mp.media_item_id = mdi.id AND mp.deleted_at IS NULL
LEFT JOIN metadata_items parent ON parent.id = mi.parent_id
LEFT JOIN metadata_items grand ON grand.id = parent.parent_id
WHERE mi.library_section_id IN ({ids}) AND mi.metadata_type IN (1, 4) AND mi.deleted_at IS NULL
  AND COALESCE(mdi.proxy_type, 0) = 0
"""
AUDIO_COUNT_SQL = """
SELECT ms.media_part_id, COUNT(*) FROM media_streams ms JOIN media_parts mp ON mp.id = ms.media_part_id
JOIN media_items mdi ON mdi.id = mp.media_item_id JOIN metadata_items mi ON mi.id = mdi.metadata_item_id
WHERE ms.stream_type_id = 2 AND ms."index" IS NOT NULL AND mi.library_section_id IN ({ids}) GROUP BY ms.media_part_id
"""
# Same stream filter as the loudness job (indexed audio only), so the counts agree with what it will analyse.
# Only streams that may carry loudness (plain or URL-encoded 'ln:'): keeps memory small
# (thousands of rows, not hundreds of thousands).
LOUDNESS_STREAMS_SQL = """
SELECT ms.media_part_id, ms.extra_data FROM media_streams ms JOIN media_parts mp ON mp.id = ms.media_part_id
JOIN media_items mdi ON mdi.id = mp.media_item_id JOIN metadata_items mi ON mi.id = mdi.metadata_item_id
WHERE ms.stream_type_id = 2 AND ms."index" IS NOT NULL
  AND (ms.extra_data LIKE '%ln:%' OR ms.extra_data LIKE '%ln\\%3A%' ESCAPE '\\')
  AND mi.library_section_id IN ({ids})
"""
# Plex keeps chapters as taggings of tag type 9; a chapter has a thumbnail when its thumb_url is set (by Plex or
# this app). One row per item keeps memory small: the chapter count plus "index:start" of each chapter without one.
CHAPTERS_SQL = """
SELECT t.metadata_item_id, COUNT(*),
       GROUP_CONCAT(CASE WHEN COALESCE(t.thumb_url, '') = '' THEN t."index" || ':' || t.time_offset END)
FROM taggings t JOIN tags ON tags.id = t.tag_id AND tags.tag_type = 9
JOIN metadata_items mi ON mi.id = t.metadata_item_id
WHERE mi.library_section_id IN ({ids}) AND mi.metadata_type IN (1, 4) AND mi.deleted_at IS NULL
GROUP BY t.metadata_item_id
"""
DURATIONS_SQL = """
SELECT mdi.metadata_item_id, MAX(mdi.duration) FROM media_items mdi
JOIN metadata_items mi ON mi.id = mdi.metadata_item_id
WHERE mi.library_section_id IN ({ids}) AND mdi.deleted_at IS NULL GROUP BY mdi.metadata_item_id
"""
MARKERS_SQL = """
SELECT tg.metadata_item_id, tg.text FROM taggings tg JOIN tags t ON t.id = tg.tag_id
WHERE t.tag_type = 12 AND tg.text IN ('intro', 'credits')
"""


def query_rows(
    conn: sqlite3.Connection, section_ids: list[int], *, want_markers: bool, want_chapters: bool
) -> dict[str, Iterator[tuple]]:
    """The queries for these sections, each run only when its rows are first read, one after another.

    The app reads them straight into its lookups: holding every row in a second copy first costs a big library
    about a third more memory on every Health check.

    Args:
        conn: A read-only connection to Plex's database, open until every query has been read.
        section_ids: Plex library section ids (whole numbers, bound as parameters).
        want_markers: Also read the intro/credits marker rows.
        want_chapters: Also read chapters and the items' durations.

    Returns:
        ``{"parts", "audio_counts", "loudness_streams", "markers", "chapters", "durations"}``, each SQLite's row
        tuples (``markers``/``chapters``/``durations`` empty when not wanted).
    """
    marks = ",".join("?" * len(section_ids))

    def rows(sql: str, params: list[int]) -> Iterator[tuple]:
        yield from conn.execute(sql.format(ids=marks), params)

    return {
        "parts": rows(PARTS_SQL, section_ids),
        "audio_counts": rows(AUDIO_COUNT_SQL, section_ids),
        "loudness_streams": rows(LOUDNESS_STREAMS_SQL, section_ids),
        "markers": rows(MARKERS_SQL, []) if want_markers else iter(()),
        "chapters": rows(CHAPTERS_SQL, section_ids) if want_chapters else iter(()),
        "durations": rows(DURATIONS_SQL, section_ids) if want_chapters else iter(()),
    }


def read_rows(conn: sqlite3.Connection, section_ids: list[int], *, want_markers: bool, want_chapters: bool) -> dict:
    """Run the queries for these sections, every row a list: the rows of a Plex database node's answer, which it
    compresses as it reads them instead of holding them all as rows (``nodes.server.plex_db``).

    Args:
        conn: A read-only connection to Plex's database.
        section_ids: Plex library section ids (whole numbers, bound as parameters).
        want_markers: Also read the intro/credits marker rows.
        want_chapters: Also read chapters and the items' durations.

    Returns:
        ``query_rows``' keys, each a list of row lists exactly as SQLite returned them.
    """
    queries = query_rows(conn, section_ids, want_markers=want_markers, want_chapters=want_chapters)
    return {key: [list(row) for row in rows] for key, rows in queries.items()}


def section_ids_from_json(raw: object) -> list[int]:
    """Validate a request's section ids.

    Raises:
        ValueError: Not a list of at most ``MAX_SECTIONS`` positive whole numbers.
    """
    if not isinstance(raw, list) or not raw or len(raw) > MAX_SECTIONS:
        raise ValueError(f"section_ids must be a list of 1 to {MAX_SECTIONS} section ids")
    if any(type(value) is not int or value < 1 for value in raw):
        raise ValueError("section_ids must be positive whole numbers")
    return list(raw)
