"""Scratch: the one-time matching cost on sflix. New pairs = pairs of a cross-disk group whose two files sit in
different folders (same-folder pairs are cached already, v10 keeps pair version 9); time per pair measured on real
fingerprints of the sample's groups (one CPU thread)."""

import itertools
import json
import os
import random
import time

import lib
from lib import S

split = json.load(open(lib.HERE / "split.json"))
new_pairs = total_files = 0
for season in split:
    counts = list(season["folders"].values())
    n = sum(counts)
    total_files += n
    if n <= S.MAX_GROUP_EPISODES:
        new_pairs += (n * (n - 1) - sum(c * (c - 1) for c in counts)) // 2
    else:  # each file's 40 nearest: at most 39 partners, the share across folders as the counts give it
        cross = 1 - sum(c * (c - 1) for c in counts) / (n * (n - 1))
        new_pairs += round(n * (S.MAX_GROUP_EPISODES - 1) / 2 * cross)
sample = json.load(open(lib.HERE / "sample.json"))
paths = []
for season in sample:
    for folder in season["folders"]:
        paths += [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.endswith((".mkv", ".mp4"))]
points = {p: lib.points(p) for p in paths}
by_season = {}
for p in paths:
    by_season.setdefault(os.path.dirname(os.path.dirname(p)).split("/")[-1] + os.path.basename(os.path.dirname(p)),
                         []).append(p)  # fmt: skip
pairs = [pair for group in by_season.values() for pair in itertools.combinations(sorted(group), 2)]
random.Random(1).shuffle(pairs)
pairs = [(a, b) for a, b in pairs if points[a] is not None and points[b] is not None][:300]
t0 = time.process_time()
for a, b in pairs:
    S.season_pair_runs(points[a], points[b])
per_pair = (time.process_time() - t0) / len(pairs)
print(f"split seasons {len(split)}, files {total_files}, new cross-folder pairs {new_pairs:,}")
print(
    f"{per_pair * 1000:.1f} ms CPU per pair over {len(pairs)} real pairs -> {new_pairs * per_pair / 3600:.1f} CPU-hours"
)
