"""Scratch: the tree's cross-disk group equals the scratch one on every truth-set and sample file; timing per call."""

import json
import os
import time

import lib
from app_group import app_group

files = set()
for mode in ("accused", "lists", "scale_clean", "chap_clean"):
    files |= {f for f in lib.load_truth(mode) if os.path.exists(f)}
for season in json.load(open(lib.HERE / "sample.json")):
    files |= set(season["episodes"])
diff = 0
t0 = time.time()
for f in sorted(files):
    a, b = tuple(app_group(f)), tuple(lib.across_disks(f))
    if a != b:
        diff += 1
        print("DIFF", f, len(a), len(b), sorted(set(a) ^ set(b))[:4])
print(
    len(files), "files,", diff, "differ;", f"{(time.time() - t0) / len(files) * 1000:.0f} ms per file (both groupings)"
)
