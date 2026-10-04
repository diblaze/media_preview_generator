"""ILA and Killer Cases target files from the final audit's markers.db copy."""

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
db = sqlite3.connect(f"file:{HERE.parent / 'final-audit/db/markers.db'}?mode=ro", uri=True)
rows = db.execute(
    "select f.canonical_path, f.duration_ms, e.start_ms, e.end_ms from files f left join evidence e on "
    "e.file_id=f.id and e.source='credits_text' where f.canonical_path like '%I Live Alone%' and f.missing_since is "
    "null order by f.canonical_path"
).fetchall()
print(len(rows))
kc = db.execute(
    "select f.canonical_path, f.duration_ms, e.start_ms, e.end_ms from files f join evidence e on e.file_id=f.id and "
    "e.source='credits_text' where f.canonical_path like '%Killer Cases%'"
).fetchall()
for r in kc:
    print(r)
json.dump({"ila": rows, "kc": kc}, open(HERE / "targets.json", "w"), indent=0)
