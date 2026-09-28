"""Per answered file: the chosen run and the story before it, with a verdict against the lane's truth.

Usage: feat2.py <ct.json> [...] -> writes feat2_<first>.json; prints nothing (see table.py).
"""

import json
import os
import sys
from statistics import median

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "base"))
from media_preview_generator.markers.credits import rule_j  # noqa: E402

TRUTH = json.load(open(os.path.join(HERE, "truth.json")))
TOL = 5.0


def rows(x):
    return [tuple(r[:3]) + (tuple(tuple(b) for b in r[3]),) for r in x]


def verdict(start, path, r):
    t = TRUTH.get(path)
    if t is None and r.get("truth") is not None:
        tv = r["truth"]["start"] if isinstance(r["truth"], dict) else r["truth"]
        t = {"lo": tv, "hi": tv}
    if "I Live Alone" in path:
        return "ila"
    if t is None:
        return "?"
    if start is None:
        return "none"
    if t["lo"] - TOL <= start <= t["hi"] + TOL:
        return "right"
    return "early" if start < t["lo"] - TOL else "late"


def h(b):
    return b[3] - b[1] + 1


def features(path, r):
    key = rows(r["key"])
    ov = [tuple(b) for b in r["overlays"]]
    src = rows(r["runs"]) if r.get("runs") else key
    own = rule_j.without_overlays(src, ov)
    coarse = rule_j.coarse_start(src, without=own, black_reads=r["scale"] == 1)
    if coarse is None:
        return None
    dark = rule_j.RULE_J.dark
    order = sorted(range(len(own)), key=lambda i: own[i][0])
    run_end = rule_j.coarse_end_s(own, coarse)
    span = [own[i] for i in order if coarse.pts_s <= own[i][0] <= run_end]
    before = [own[i] for i in order if own[i][0] < coarse.pts_s]
    before_raw = [x for x in key if x[0] < coarse.pts_s]
    credit = [x for x in span if rule_j.is_credit(x)]
    boxes = [b for x in credit for b in rule_j.boxes_of(x)]
    lit = [x for x in span if x[2] >= dark]
    dur = r["duration"] / 1000
    t0 = min(x[0] for x in key)
    f = {
        "start": r["start_s"],
        "coarse": coarse.pts_s,
        "end_s": r["end_s"],
        "to_end": dur - r["start_s"],
        "run_len": run_end - coarse.pts_s,
        "after_run": dur - run_end,
        "story_s": coarse.pts_s - t0,
        "gop": median([b[0] - a[0] for a, b in zip(sorted(key)[:-1], sorted(key)[1:], strict=False)] or [0]),
        "n_span": len(span),
        "any_before": sum(1 for x in before_raw if x[1] >= 1) / max(1, len(before_raw)),
        "credit_rate_before": sum(1 for x in before if rule_j.is_credit(x)) / max(1, len(before)),
        "credit_rate_span": len(credit) / max(1, len(span)),
        "text_share_span": sum(1 for x in span if x[1] >= 1) / max(1, len(span)),
        "lit_share_span": len(lit) / max(1, len(span)),
        "lit_blank_span": sum(1 for x in lit if x[1] == 0) / max(1, len(span)),
        "lit_credit_share": sum(1 for x in credit if x[2] >= dark) / max(1, len(credit)),
        "h_med": median([h(b) for b in boxes]) if boxes else 0,
        "h_small": sum(1 for b in boxes if h(b) <= 16) / max(1, len(boxes)),
        "runs_before": sum(1 for a, b in rule_j.credit_runs(src) if max(x[0] for x in src[a : b + 1]) < coarse.pts_s),
        "scale": r["scale"],
    }
    band = rule_j.band_of(own, coarse.index if coarse.run_index is None else coarse.run_index, coarse.end_index)
    xs = [median((b[0] + b[2] + 1) / 2 for b in rule_j.boxes_of(x)) for x in credit if rule_j.boxes_of(x)]
    ys = [median((b[1] + b[3] + 1) / 2 for b in rule_j.boxes_of(x)) for x in credit if rule_j.boxes_of(x)]
    f["band_share"] = sum(1 for x in credit if band is not None and rule_j.in_band(x, band)) / max(1, len(credit))
    xs.sort()
    ys.sort()
    f["x_iqr"] = (xs[3 * len(xs) // 4] - xs[len(xs) // 4]) if len(xs) >= 4 else 0
    f["y_iqr"] = (ys[3 * len(ys) // 4] - ys[len(ys) // 4]) if len(ys) >= 4 else 0
    story = 0.0
    for a, b in zip(span, span[1:], strict=False):
        if a[2] >= dark and not rule_j.is_credit(a):
            story += b[0] - a[0]
    f["lit_story_s"] = story
    blank = 0.0
    for a, b in zip(span, span[1:], strict=False):
        if a[2] >= dark and a[1] == 0:
            blank += b[0] - a[0]
    f["lit_blank_s"] = blank
    f["n_lit_blank"] = sum(1 for x in span if x[2] >= dark and x[1] == 0)
    gaps = sorted(b[0] - a[0] for a, b in zip(credit, credit[1:], strict=False))
    f["credit_gap_med"] = gaps[len(gaps) // 2] if gaps else 0
    f["credit_gap_max"] = gaps[-1] if gaps else 0
    f["verdict"] = verdict(r["start_s"], path, r)
    f["set"] = r.get("set")
    return f


if __name__ == "__main__":
    out = {}
    for fn in sys.argv[1:]:
        d = json.load(open(fn))
        for path, r in d.items():
            if "error" in r or r.get("start_s") is None:
                continue
            ft = features(path, r)
            if ft is not None:
                out[path] = ft
    name = os.path.basename(sys.argv[1]).replace(".json", "")
    json.dump(out, open(os.path.join(HERE, f"feat2_{name}.json"), "w"))
    print(len(out))
