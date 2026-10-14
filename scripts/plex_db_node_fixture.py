"""Write a small Plex config folder for booting the Plex database node image against (CI's ``marker-agent-image``).

Standard library only, so it runs on a bare CI runner without the app installed. The folder holds Plex's library
database (the 1.43 schema in ``tests/fixtures/markers/plex_schema_1_43.sql``, WAL mode as Plex keeps it) with one
movie in library section 1, and a ``Preferences.xml`` naming the server.

Usage:
    python3 scripts/plex_db_node_fixture.py <plex config folder> [machine identifier]
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[1] / "tests/fixtures/markers/plex_schema_1_43.sql"
DB_RELATIVE = "Plug-in Support/Databases/com.plexapp.plugins.library.db"
DEFAULT_MACHINE = "ci-plex"


def build(folder: Path, machine_identifier: str = DEFAULT_MACHINE) -> Path:
    """Create the folder's database and Preferences.xml.

    Args:
        folder: Plex's config folder (``PLEX_CONFIG_DIR``); created when missing.
        machine_identifier: The ``ProcessedMachineIdentifier`` Preferences.xml carries.

    Returns:
        The database path.
    """
    db = folder / DB_RELATIVE
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA.read_text())
        conn.execute(
            "INSERT INTO metadata_items (id, library_section_id, metadata_type, title) VALUES (1, 1, 1, 'Film')"
        )
        conn.execute("INSERT INTO media_items (id, metadata_item_id, duration) VALUES (1, 1, 5400000)")
        conn.execute("INSERT INTO media_parts (id, media_item_id, file, hash) VALUES (1, 1, '/media/film.mkv', 'ab12')")
        conn.execute(
            'INSERT INTO media_streams (id, stream_type_id, "index", codec, media_part_id, media_item_id) '
            "VALUES (1, 2, 1, 'aac', 1, 1)"
        )
        conn.commit()
    finally:
        conn.close()
    (folder / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{machine_identifier}"/>\n')
    return db


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    print(build(Path(sys.argv[1]), *sys.argv[2:]))
