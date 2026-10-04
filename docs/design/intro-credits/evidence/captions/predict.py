"""Which scale-1 answers of a base ct run the work tree's captions_all_through would refuse, from their stored rows
(scale-2 answers are listed as 'scale 2' for the work run to settle). Usage: predict.py <ct.json> [set]"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("CAPTIONS_WORK") or str(Path(__file__).resolve().parents[5]))
from media_preview_generator.markers.credits import rule_j  # noqa: E402


def rows(x):
    return [tuple(r[:3]) + (tuple(tuple(b) for b in r[3]),) for r in x]


d = json.load(open(sys.argv[1]))
want = sys.argv[2] if len(sys.argv) > 2 else None
hit = scale2 = n = 0
for path, r in d.items():
    if "error" in r or r.get("start_s") is None or (want and r.get("set") != want):
        continue
    n += 1
    if r["scale"] != 1:
        scale2 += 1
        continue
    key = rows(r["key"])
    ov = [tuple(b) for b in r["overlays"]]
    own = rule_j.without_overlays(key, ov)
    c = rule_j.coarse_start(key, without=own)
    if c is None or rule_j.text_all_through(key, c):
        print("?? no coarse / refused already", os.path.basename(path)[:60])
        continue
    if rule_j.captions_all_through(key, own, c, rule_j.coarse_end_s(own, c)):
        hit += 1
        print("REFUSED", r["start_s"], os.path.basename(path)[:70])
print(f"answered {n}, scale-1 refused {hit}, scale-2 {scale2}")
