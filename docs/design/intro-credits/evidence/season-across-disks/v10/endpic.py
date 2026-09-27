"""Scratch: why an episode's top cluster fails the guards. Usage: endpic.py show season episode_code [own]"""

import sys

import lib
from find import find_episode
from lib import M, S, end_picture

path = find_episode(sys.argv[1], int(sys.argv[2]))
own = len(sys.argv) > 4 and sys.argv[4] == "own"
eps = S.season_group(path).episodes if own else lib.across_disks(path)
g = lib.Group(eps)
target = next(f for f in g.files if sys.argv[3] in f)
cands = M.intro_candidates(M.file_hits(target, g.files, g.runs_between))
c = cands[0]
seg = c.segment
print("top", seg, "cut", S.cut_by_window(seg, g.points[target]) if hasattr(S, "cut_by_window") else "-")
print("needs core", S.needs_dense_core(seg), "core", round(S.dense_core_s(target, c, g.points), 1))
for hit in end_picture.partners(c.members):
    off = hit.partner_start_s - hit.start_s
    print("  partner", hit.partner.rsplit("/", 1)[1][:50], "hit", round(hit.start_s, 1), round(hit.end_s, 1),
          "offset", round(off, 2), "share", lib.share(target, hit.partner, seg.start_s, seg.end_s, off))  # fmt: skip
lib.save()
