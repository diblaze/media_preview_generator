"""Scratch: per-file verdict changes, dev (own folder) -> this tree (across disks), on the four sets."""

import collections
import os
import pickle

import lib

base = pickle.load(open(lib.HERE / "sets_base.pkl", "rb"))
app = pickle.load(open(lib.HERE / "sets_app.pkl", "rb"))
for mode in ("accused", "lists", "scale_clean", "chap_clean"):
    moves = collections.Counter()
    for (m, f), row in sorted(app.items()):
        if m != mode:
            continue
        before, after = base[(m, f)]["v_own"], row["v_across"]
        if before != after:
            moves[(before, after)] += 1
            name = os.path.basename(f).split(" [")[0]
            print(f"  {mode:11} {name[:70]:70} {before} -> {after}  {base[(m, f)]['own']} -> {row['across']}")
    print(mode, dict(moves))
