"""Scratch: the season step (app grouping, every disk) on the truth sets and the split sample.

Usage: CODE=... CAP=... sruns.py LABEL mode... [sample]   -> season_LABEL.pkl, per-file answers and verdicts.
Fails fast: with FAILFAST=reference.pkl, stops at the first file whose verdict is worse than the reference's.
"""

import json
import os
import pickle
import sys
import time

import slib

label, modes = sys.argv[1], sys.argv[2:]
reference = pickle.load(open(os.environ["FAILFAST"], "rb")) if os.environ.get("FAILFAST") else None
RANK = {"useful": 2, "none-ok": 2, "missed": 1, "wrong": 0}
groups: dict[tuple, slib.Group] = {}


def answer(f):
    eps = slib.app_group(f)
    key = tuple(eps)
    if key not in groups:
        groups[key] = slib.Group(eps)
    seg = groups[key].answer(f)
    return (None if seg is None else (round(seg.start_s, 1), round(seg.end_s, 1), seg.support)), len(groups[key].files)


out = {}
for mode in modes:
    t0 = time.time()
    if mode == "sample":
        sample = json.load(open(slib.HERE / "sample.json"))
        for season in sample:
            for f in season["episodes"]:
                a, n = answer(f)
                out[("sample", f)] = dict(truth=None, answer=a, n=n, verdict=None)
                old = reference.get(("sample", f)) if reference else None
                if old and old["answer"] != a:
                    print(f"   sample {os.path.basename(f)[:60]:60} {old['answer']} -> {a}", flush=True)
        print(f"{label} sample done ({time.time() - t0:.0f}s)", flush=True)
        slib.save()
        continue
    truth = slib.load_truth(mode)
    rows = []
    for i, (f, tr) in enumerate(sorted(truth.items())):
        if not os.path.exists(f):
            continue
        a, n = answer(f)
        v = slib.judge(a, tr)
        rows.append(v)
        out[(mode, f)] = dict(truth=tr, answer=a, n=n, verdict=v)
        old = reference.get((mode, f)) if reference else None
        if old and old["answer"] != a:
            print(f"   {mode:11} {os.path.basename(f)[:58]:58} truth {tr} {old['verdict']} {old['answer']} -> {v} {a}",
                  flush=True)  # fmt: skip
            if RANK[v] < RANK[old["verdict"]] and os.environ.get("FAILFAST_STOP"):
                print("FAIL FAST: worse verdict", flush=True)
                slib.save()
                pickle.dump(out, open(slib.HERE / f"season_{label}.pkl", "wb"))
                sys.exit(3)
        if i % 40 == 39:
            slib.save()
    print(f"{label} {mode:12} {slib.tally(rows)}   ({time.time() - t0:.0f}s)", flush=True)
    slib.save()
pickle.dump(out, open(slib.HERE / f"season_{label}.pkl", "wb"))
