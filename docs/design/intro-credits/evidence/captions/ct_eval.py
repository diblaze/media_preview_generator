"""Credit text starts, base vs work, per set: right (within 5 s of the truth, or inside a frame check's lo..hi), early,
late, none, no truth; and every file whose start changed.

Usage: ct_eval.py <base.json> <work.json>
"""

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
TRUTH = json.load(open(os.path.join(HERE, "truth.json")))
base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
# The v8 lane's frame checks of epilogue files (their truths in the sets sit on the epilogue).
FRAME = {
    "Ocean with David Attenborough": (4774.0, 4776.0),
    "To Dye For The Documentary": (4926.0, 4926.0),
    "Gandhari (2026)": (6531.0, 6544.0),
    "A Beautiful Imperfection": (6123.0, 6135.9),
    "#SKYKING": (5289.0, 5289.0),
}
TOL = 5.0


def bounds(path, r):
    name = os.path.basename(path)
    for key, lohi in FRAME.items():
        if key in name:
            return lohi
    t = r.get("truth")
    if isinstance(t, dict):
        t = t.get("start")
    if t is not None:
        return (float(t), float(t))
    if path in TRUTH:
        return (TRUTH[path]["lo"], TRUTH[path]["hi"])
    return None


def verdict(start, lohi):
    if lohi is None:
        return "no truth"
    if start is None:
        return "none"
    lo, hi = lohi
    if lo - TOL <= start <= hi + TOL:
        return "right"
    return "early" if start < lo - TOL else "late"


tally: dict[str, list[Counter]] = defaultdict(lambda: [Counter(), Counter()])
changes = []
for path in sorted(set(base) & set(work)):
    b, w = base[path], work[path]
    if "error" in b or "error" in w:
        print("ERROR", os.path.basename(path)[:60], b.get("error"), w.get("error"))
        continue
    lohi = bounds(path, b)
    vb, vw = verdict(b.get("start_s"), lohi), verdict(w.get("start_s"), lohi)
    label = b.get("set", "?")
    tally[label][0][vb] += 1
    tally[label][1][vw] += 1
    if b.get("start_s") != w.get("start_s") or b.get("end_s") != w.get("end_s"):
        changes.append((label, os.path.basename(path)[:55], b.get("start_s"), b.get("end_s"), w.get("start_s"),
                        w.get("end_s"), w.get("scale"), lohi and lohi[0], vb, vw))  # fmt: skip
keys = ("right", "early", "late", "none", "no truth")
for label, (cb, cw) in sorted(tally.items()):
    print(
        f"{label:20} base "
        + " ".join(f"{k} {cb[k]}" for k in keys)
        + " | work "
        + " ".join(f"{k} {cw[k]}" for k in keys)
    )
print(len(changes), "changed")
for row in changes:
    print("  ", row)
