"""Scratch: tally selection variants on the truth sets. Usage: run_variants.py GROUPING(own|across) FLAGS,FLAGS... modes..."""

import collections
import os
import pickle
import sys
import time

import lib
import variants
from lib import S

grouping, flag_sets, modes = sys.argv[1], sys.argv[2].split(","), sys.argv[3:]
flag_sets = ["" if f == "-" else f for f in flag_sets]
groups: dict[tuple, lib.Group] = {}
memos: dict[tuple, dict] = {}
results = {}
for mode in modes:
    t0 = time.time()
    truth = lib.load_truth(mode)
    tallies = {f: [] for f in flag_sets}
    for i, (f, tr) in enumerate(sorted(truth.items())):
        if not os.path.exists(f):
            continue
        eps = tuple(S.season_group(f).episodes if grouping == "own" else lib.across_disks(f))
        if eps not in groups:
            groups[eps] = lib.Group(eps)
            memos[eps] = {}
        g = groups[eps]
        got = {}
        for flags in flag_sets:
            seg = variants.pick(g, f, flags, memos[eps])
            got[flags] = None if seg is None else (round(seg.start_s, 1), round(seg.end_s, 1), seg.support)
            tallies[flags].append(lib.judge(got[flags], tr))
        results[(mode, f)] = (tr, got, len(g.files))
        base = flag_sets[0]
        for flags in flag_sets[1:]:
            if lib.judge(got[flags], tr) != lib.judge(got[base], tr):
                print(f"   {mode:11} [{flags:4}] {os.path.basename(f)[:56]:56} truth {tr} "
                      f"{lib.judge(got[base], tr)} {got[base]} -> {lib.judge(got[flags], tr)} {got[flags]} ({len(g.files)})",
                      flush=True)  # fmt: skip
        if i % 40 == 39:
            lib.save()
    for flags in flag_sets:
        print(f"{grouping:6} {mode:12} [{flags or '-':4}] {lib.tally(tallies[flags])}", flush=True)
    print(f"   ({time.time() - t0:.0f}s)", flush=True)
    lib.save()
pickle.dump(results, open(lib.HERE / f"variants_{grouping}.pkl", "wb"))
