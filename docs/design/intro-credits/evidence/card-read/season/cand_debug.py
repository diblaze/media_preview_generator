"""Scratch: one episode's ranked season clusters, with quorum and guards, in one tree.

Usage: cand_debug.py base|work <path>
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = "/home/data/workspace/plex_generate_vid_previews"
os.environ["CODE"] = os.path.join(HERE, "base") if sys.argv[1] == "base" else WORK
os.environ["CAP"] = ""

import slib  # noqa: E402
from slib import M, S  # noqa: E402

import pickle  # noqa: E402

target = next(k[1] for k in pickle.load(open(os.path.join(HERE, "season_base.pkl"), "rb")) if sys.argv[2] in k[1])
eps = slib.app_group(target)
group = slib.Group(eps)
files = group.files
others = len(files) - 1
print("group", len(files), "window of target", len(group.points[target]) * 0.1238)
hits = M.file_hits(target, files, group.runs_between)
candidates = M.intro_candidates(hits)
memo: dict = {}
seen = set()
for c in candidates:
    if (round(c.segment.start_s), round(c.segment.end_s)) in seen:
        continue
    seen.add((round(c.segment.start_s), round(c.segment.end_s)))
    if len(seen) > 14:
        break
    seg = c.segment
    q = M.meets_quorum(seg.support, others)
    oq = M.meets_opening_quorum(target, c, files, group.runs_between, S._season_and_episode, memo)
    g = S._passes_guards(target, c, group.points, group.end_picture_passes(target))
    print(f"{seg.start_s:8.1f} {seg.end_s:8.1f} support {seg.support:2} quorum {q} opening {oq} guards {g}")
    if seg.support == 1:
        print("     partners", sorted({(os.path.basename(h.partner)[:40], round(h.partner_start_s, 1)) for h in c.members}))
print("answer", group.answer(target))
