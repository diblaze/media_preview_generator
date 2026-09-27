"""Scratch: the season step on the four truth sets, own folder vs across disks. Usage: run_sets.py LABEL mode...

GROUPING=scratch uses lib.across_disks; GROUPING=app uses the tree's own cross-disk grouping (APP_GROUP)."""

import collections
import os
import pickle
import sys
import time

import lib
from lib import S

label, modes = sys.argv[1], sys.argv[2:]
GROUPING = os.environ.get("GROUPING", "scratch")


def across(f):
    if GROUPING == "scratch":
        return lib.across_disks(f)
    from app_group import app_group

    return app_group(f)


groups: dict[tuple, lib.Group] = {}


def answer(f, episodes):
    key = tuple(episodes)
    if key not in groups:
        groups[key] = lib.Group(episodes)
    seg = groups[key].answer(f)
    return None if seg is None else (round(seg.start_s, 1), round(seg.end_s, 1), seg.support), len(groups[key].files)


out = {}
for mode in modes:
    t0 = time.time()
    truth = lib.load_truth(mode)
    rows = {"own": [], "across": []}
    for i, (f, tr) in enumerate(sorted(truth.items())):
        if not os.path.exists(f):
            continue
        own_eps = S.season_group(f).episodes
        across_eps = across(f)
        a_own, n_own = answer(f, own_eps)
        a_x, n_x = (a_own, n_own) if tuple(across_eps) == tuple(own_eps) else answer(f, across_eps)
        v_own, v_x = lib.judge(a_own, tr), lib.judge(a_x, tr)
        rows["own"].append(v_own)
        rows["across"].append(v_x)
        out[(mode, f)] = dict(truth=tr, own=a_own, n_own=n_own, across=a_x, n_across=n_x, v_own=v_own, v_across=v_x)
        if v_own != v_x:
            print(f"   {mode:11} {os.path.basename(f)[:58]:58} truth {tr} {v_own} {a_own} ({n_own}) -> {v_x} {a_x} ({n_x})",
                  flush=True)  # fmt: skip
        if i % 40 == 39:
            lib.save()
    print(f"{label} {mode:12} own {lib.tally(rows['own'])}   across {lib.tally(rows['across'])}   ({time.time() - t0:.0f}s)",
          flush=True)  # fmt: skip
    lib.save()
    pickle.dump(out, open(lib.HERE / f"sets_{label}.pkl", "wb"))
