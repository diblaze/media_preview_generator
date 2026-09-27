"""Scratch: season folders of the TV library split across its disks (same show by tvdb id else name, same season
number). Read-only listing. Writes split.json: [{show, season, folders: {folder: n_videos}}]."""

import collections
import json
import os
import re

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov", ".mpg", ".mpeg", ".webm", ".m2ts")
SEASON = re.compile(r"^(?:season|series|staffel|saison)\s*(\d{1,4})$", re.I)
TVDB = re.compile(r"\{tvdb-(\d+)\}")

seasons = collections.defaultdict(dict)
for root in ROOTS:
    base = f"{root}/TV Shows"
    for show in os.listdir(base):
        sd = os.path.join(base, show)
        if not os.path.isdir(sd):
            continue
        m = TVDB.search(show)
        key = ("tvdb", m.group(1)) if m else ("name", show)
        for sname in os.listdir(sd):
            sm = SEASON.match(sname)
            if not sm:
                continue
            folder = os.path.join(sd, sname)
            try:
                n = sum(1 for f in os.listdir(folder) if f.lower().endswith(VIDEO))
            except OSError:
                continue
            if n:
                seasons[(key, int(sm.group(1)))][folder] = n
split = [
    {"show": k[0][1], "season": k[1], "folders": v}
    for k, v in sorted(seasons.items(), key=lambda kv: str(kv[0]))
    if len(v) > 1
]
names_differ = sum(1 for s in split if len({os.path.basename(os.path.dirname(f)) for f in s["folders"]}) > 1)
print("season folders", sum(len(v) for v in seasons.values()), "seasons", len(seasons), "split", len(split),
      "files in split", sum(sum(s["folders"].values()) for s in split), "split with differing show folder names",
      names_differ)  # fmt: skip
json.dump(split, open(os.path.join(os.path.dirname(__file__), "split.json"), "w"), indent=0)
