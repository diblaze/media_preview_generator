"""Quantiles of each feature by verdict group. Usage: table.py feat2_a.json [feat2_b.json ...] [--list col>val]"""

import json
import os
import sys
from collections import defaultdict

cols = ["to_end", "run_len", "story_s", "gop", "any_before", "credit_rate_before", "credit_rate_span",
        "text_share_span", "lit_share_span", "lit_blank_span", "lit_credit_share", "h_med", "h_small",
        "runs_before", "band_share", "x_iqr", "y_iqr", "lit_story_s", "credit_gap_med", "credit_gap_max"]  # fmt: skip
files = [a for a in sys.argv[1:] if a.endswith(".json")]
data = {}
for f in files:
    data.update(json.load(open(f)))
groups = defaultdict(list)
for f in data.values():
    groups[f["verdict"]].append(f)


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))] if v else float("nan")


for c in cols:
    print(c)
    for g, fs in sorted(groups.items()):
        v = [x[c] for x in fs]
        print(f"   {g:6} n={len(v):4} " + " ".join(f"{q(v, p):7.2f}" for p in (0, 0.05, 0.25, 0.5, 0.75, 0.95, 1)))
if "--list" in sys.argv:
    expr = sys.argv[sys.argv.index("--list") + 1]
    for p, f in sorted(data.items(), key=lambda kv: kv[1]["verdict"]):
        if eval(expr, {}, f):  # noqa: S307 - scratch
            print(f"{f['verdict']:6} {str(f['set'])[:9]:9} {os.path.basename(p)[:50]:50} " + " ".join(
                f"{c[:6]}={f[c]:.2f}" for c in cols))  # fmt: skip
