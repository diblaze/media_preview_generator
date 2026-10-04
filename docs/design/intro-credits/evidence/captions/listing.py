"""How many sflix files one tree's version re-run lists on the final audit's markers.db copy, and for what.

Usage: listing.py <tree>
"""

import os
import shutil
import sys
from collections import Counter
from pathlib import Path

tree = sys.argv[1]
sys.path.insert(0, tree)
from media_preview_generator.markers import versions  # noqa: E402
from media_preview_generator.markers.settings import load_global, validate_global  # noqa: E402
from media_preview_generator.markers.store import MarkerStore  # noqa: E402

assert versions.__file__.startswith(tree)
HERE = Path(__file__).resolve().parent
src = HERE.parent / "final-audit/db/markers.db"
copy = HERE / f"listing_{os.getpid()}.db"
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(f"{src}{suffix}"):
        shutil.copy(f"{src}{suffix}", f"{copy}{suffix}")
store = MarkerStore(str(copy))
order = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")
raw = {
    "detect": {"intro": True, "credits": True, "recap": False},
    "sources": [{"id": s, "enabled": True} for s in order],
}
due = versions.files_to_read_again(store, load_global(validate_global(raw, None)[0]))
print("listed", len(due))
print(Counter(k for v in due.values() for k in v))
ila = sum(1 for p in due if "I Live Alone" in p)
print("I Live Alone listed", ila)
store._conn.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(f"{copy}{suffix}"):
        os.remove(f"{copy}{suffix}")
