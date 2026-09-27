"""Scratch: one season group's candidates per episode. Usage: explore.py <episode path> [own]"""

import os
import re
import sys

import lib
from lib import M, S

from find import find_episode

path = find_episode(sys.argv[1], int(sys.argv[2]))
own = len(sys.argv) > 3 and sys.argv[3] == "own"
episodes = S.season_group(path).episodes if own else lib.across_disks(path)
g = lib.Group(episodes)
print(len(episodes), "episodes,", len(g.files), "audible; clock", g.clock.speed, dict(g.clock.factors))
for f in g.files:
    ep = re.search(r"S\d+E\d+", f).group(0)
    hits = M.file_hits(f, g.files, g.runs_between)
    cands = M.intro_candidates(hits)
    seen = set()
    rows = []
    for c in cands:
        k = (round(c.segment.start_s), round(c.segment.end_s))
        if k in seen:
            continue
        seen.add(k)
        rows.append(f"{c.segment.start_s:6.1f}-{c.segment.end_s:6.1f} s{c.segment.support}")
        if len(rows) >= 6:
            break
    ans = g.answer(f)
    a = "none" if ans is None else f"{ans.start_s:.1f}-{ans.end_s:.1f} ({ans.support})"
    print(
        f"{ep} {os.path.dirname(f).split('/')[1]:11} len {len(g.points[f]) * 0.1238:5.0f}s -> {a:22} | "
        + "; ".join(rows)
    )
lib.save()
