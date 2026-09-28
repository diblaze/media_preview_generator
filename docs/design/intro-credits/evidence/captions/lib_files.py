"""Every present sflix file whose credit text has a stored answer with a start (the audit's markers.db copy), by show.

Writes lib_files.json {path: is_episode}.
"""

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
db = sqlite3.connect(f"file:{HERE.parent / 'final-audit/db/markers.db'}?mode=ro", uri=True)
rows = db.execute(
    "select f.canonical_path, f.season_key is not null, e.start_ms from files f join evidence e on e.file_id=f.id "
    "and e.source='credits_text' and e.type='credits' where f.missing_since is null and e.start_ms is not null"
).fetchall()
print("with a credit text start", len(rows))
present = {p: bool(ep) for p, ep, _ in rows if os.path.exists(p)}
print("present", len(present))
shows = Counter(p.split("/")[3] for p in present)
print("shows", len(shows), shows.most_common(12))
json.dump(present, open(HERE / "lib_files.json", "w"))
