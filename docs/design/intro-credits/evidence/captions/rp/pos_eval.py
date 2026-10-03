"""The final audit's positional verdicts (pos_verdicts.json) against two replays: a verdict applies to a replay whose
decided marker of that type starts where the verdict's marker did (within 1 s). Removed markers count as none; markers
that moved count as CHECK. Also the 09-28 fresh sample (fresh_verdicts.json) the same way.

Usage: pos_eval.py replay_base.json replay_work.json
"""

import json
import sys
from collections import Counter
from pathlib import Path

SP = Path(__file__).resolve().parents[2]
base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))


def by_path(rep):
    out = {}
    for key, row in rep.items():
        t = key.split(":")[1]
        m = row["marker"] if row["status"] == "decided" else None
        out[(row["path"], t)] = m
    return out


B, W = by_path(base), by_path(work)


def verdicts():
    pos = json.load(open(SP / "final-audit/work/pos_verdicts.json"))
    for key, v in pos.items():
        path, t, start_ms, _ = key.split("|")
        yield "final", path, t, int(start_ms), v["v"], v.get("dir"), v.get("src")
    fresh = json.load(open(SP / "accuracy-after/work/fresh_verdicts.json"))
    sample = {x["sid"]: x for x in json.load(open(SP / "accuracy-after/work/fresh_sample.json"))}
    for sid, v in fresh.items():
        x = sample.get(sid)
        if x:
            yield "fresh", x["path"], x["type"], x["s"], v[0], v[2], sid


def cls(verdict, direction):
    if verdict == "right":
        return "useful"
    if verdict == "wrong" and direction in ("early",):
        return "harm"
    if verdict == "wrong":
        return "wrong"
    return "unclear"


tally = {"final": [Counter(), Counter()], "fresh": [Counter(), Counter()]}
rows = []
for audit, path, t, start_ms, verdict, direction, src in verdicts():
    mb, mw = B.get((path, t)), W.get((path, t))
    if mb is None or abs(mb[0] - start_ms) > 1000:
        continue  # the verdict judged a marker the base no longer has (a regression A/B's other side)
    before = cls(verdict, direction)
    if mw is None:
        after = "none"
    elif abs(mw[0] - mb[0]) <= 1000 and abs(mw[1] - mb[1]) <= 1000:
        after = before
    else:
        after = "CHECK"
    ila = "ila" if "I Live Alone" in path else "rest"
    tally[audit][0][(ila, before)] += 1
    tally[audit][1][(ila, after)] += 1
    if before != after:
        rows.append((audit, src, path.split("/")[-1][:50], t, before, after))
for audit, (cb, cw) in tally.items():
    for part in ("ila", "rest"):
        keys = ("useful", "wrong", "harm", "none", "unclear", "CHECK")
        print(f"{audit:5} {part:4} base " + " ".join(f"{k} {cb[(part, k)]}" for k in keys) + " | work "
              + " ".join(f"{k} {cw[(part, k)]}" for k in keys))  # fmt: skip
for r in rows:
    print("  ", r)
