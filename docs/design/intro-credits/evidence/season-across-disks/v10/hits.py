"""Scratch: one episode's hits (per partner). Usage: hits.py show season episode_code [own]"""

import os
import re
import sys

import lib
from find import find_episode
from lib import M, S

path = find_episode(sys.argv[1], int(sys.argv[2]))
own = len(sys.argv) > 4 and sys.argv[4] == "own"
eps = S.season_group(path).episodes if own else lib.across_disks(path)
g = lib.Group(eps)
target = next(f for f in g.files if sys.argv[3] in f)
ep = lambda f: re.search(r"S\d+E\d+", f).group(0)  # noqa: E731
for h in sorted(M.file_hits(target, g.files, g.runs_between), key=lambda h: (h.partner, h.start_s)):
    print(
        f"{ep(h.partner)} {h.start_s:7.1f}-{h.end_s:7.1f} ({h.end_s - h.start_s:5.1f}s) partner at {h.partner_start_s:7.1f}"
    )
print("fingerprint ends", round((len(g.points[target]) - 1) * lib.M.POINT_S if hasattr(lib.M, "POINT_S") else 0, 1),
      os.path.basename(target)[:60])  # fmt: skip
lib.save()
