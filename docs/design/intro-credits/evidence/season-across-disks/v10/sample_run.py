"""Scratch: before/after answers on the split-season sample (sample.json). Usage: sample_run.py LABEL [app]

Before: own folder, flags "-" (dev's season step). After: across disks, flags WBQC (scratch variants) — or, with "app",
the tree's own season_intro over the tree's own cross-disk group (app_group)."""

import json
import os
import sys
import time

import lib
import variants
from lib import S

label = sys.argv[1]
use_app = len(sys.argv) > 2 and sys.argv[2] == "app"
sample = json.load(open(os.path.join(lib.HERE, "sample.json")))
groups = {}
memos = {}


def group(eps):
    key = tuple(eps)
    if key not in groups:
        groups[key] = lib.Group(key)
        memos[key] = {}
    return groups[key], memos[key]


def seg(s):
    return None if s is None else [round(s.start_s, 1), round(s.end_s, 1), s.support]


out = []
t0 = time.time()
for season in sample:
    for f in season["episodes"]:
        own_eps = S.season_group(f).episodes
        if use_app:
            from app_group import app_group

            across_eps = app_group(f)
        else:
            across_eps = lib.across_disks(f)
        g_own, m_own = group(own_eps)
        g_x, m_x = group(across_eps)
        row = {
            "file": f,
            "n_own": len(g_own.files),
            "n_across": len(g_x.files),
            "before": seg(variants.pick(g_own, f, "", m_own)),
            "across_only": seg(variants.pick(g_x, f, "", m_x)),
        }
        if use_app:
            row["after"] = seg(g_x.answer(f))
        else:
            row["after"] = seg(variants.pick(g_x, f, "WBQC", m_x))
        out.append(row)
        print(f"{os.path.basename(f)[:60]:60} {row['n_own']:2}->{row['n_across']:2} before {row['before']} "
              f"across {row['across_only']} after {row['after']}", flush=True)  # fmt: skip
    lib.save()
json.dump(out, open(os.path.join(lib.HERE, f"sample_{label}.json"), "w"), indent=1)
print(f"({time.time() - t0:.0f}s)")
