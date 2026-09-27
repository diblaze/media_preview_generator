"""Scratch: per episode, the top cluster and the Q (opening quorum) details. Usage: explore_q.py show season"""
import re
import sys

import lib
import variants
from find import find_episode
from lib import M

path = find_episode(sys.argv[1], int(sys.argv[2]))
g = lib.Group(lib.across_disks(path))
memo = {}
ep = lambda f: re.search(r"S\d+E\d+", f).group(0)
for f in g.files:
    cands = M.intro_candidates(M.file_hits(f, g.files, g.runs_between))
    if not cands:
        print(ep(f), "no clusters")
        continue
    c = cands[0]
    partners = {h.partner for h in c.members}
    rest = [x for x in g.files if x != f and x not in partners]
    rs = set(rest)
    opened = {y for y in rest if variants._opening(variants._best_within(g, y, rs - {y}, memo), len(rest) - 1)}
    shared = [p for p in partners if variants._opening(variants._best_within(g, p, opened - {p}, memo), len(opened))]
    top_rest = {}
    for y in sorted(opened):
        b = variants._best_within(g, y, rs - {y}, memo)
        top_rest[ep(y)] = (round(b.segment.start_s), round(b.segment.end_s), b.segment.support)
    print(f"{ep(f)} top {c.segment.start_s:.1f}-{c.segment.end_s:.1f} s{c.segment.support} partners {sorted(ep(p) for p in partners)}")
    print(f"      opened {len(opened)} {top_rest}; partners sharing it {sorted(ep(p) for p in shared)}")
    pick = variants.pick(g, f, "Q", memo)
    print("      Q pick", None if pick is None else (round(pick.start_s, 1), round(pick.end_s, 1), pick.support))
lib.save()
