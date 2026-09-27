"""Scratch: the one-time cost on sflix's markers.db copy (today, read-only): for every episode with a season audio answer,
its v10 group (this tree's grouping over the four TV disks), the members never fingerprinted (CPU ffmpeg each) and the
pairs never matched (different folders). Pair time from cost.py (6.3 ms), fingerprint time measured by the harness."""

import sqlite3

import lib
from app_group import app_group

DB = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/job6742/markers.db"
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
answered = [r[0] for r in db.execute(
    "select f.canonical_path from files f join detector_runs d on d.file_id=f.id and d.source='season_audio'")]  # fmt: skip
printed = {r[0] for r in db.execute(
    "select f.canonical_path from files f join fingerprints p on p.file_id=f.id and p.window='intro'")}  # fmt: skip
groups = {}
for path in answered:
    groups.setdefault(tuple(app_group(path)), set()).add(path)
new_members = set()
new_pairs = set()
split_groups = 0
for episodes in groups:
    folders = {e.rsplit("/", 1)[0] for e in episodes}
    if len(folders) > 1:
        split_groups += 1
    new_members |= {e for e in episodes if e not in printed}
    for i, a in enumerate(episodes):
        for b in episodes[i + 1 :]:
            if a.rsplit("/", 1)[0] != b.rsplit("/", 1)[0]:
                new_pairs.add((a, b))
print(f"answers {len(answered)}, groups {len(groups)} ({split_groups} across disks), members to fingerprint "
      f"{len(new_members)}, new pairs {len(new_pairs):,} -> {len(new_pairs) * 0.0063 / 60:.1f} CPU-min matching")  # fmt: skip
