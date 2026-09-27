"""Scratch: a season's answers before (dev: own folder) and after (this tree: across disks). Needs CODE=base for
"before"; run twice and diff. Usage: season_before_after.py show season own|across"""

import json
import re
import sys

import lib
from find import find_episode
from lib import S

path = find_episode(sys.argv[1], int(sys.argv[2]))
mode = sys.argv[3]
files = lib.Group(lib.across_disks(path)).files
out = {}
groups = {}
for f in files:
    eps = tuple(S.season_group(f).episodes if mode == "own" else lib.across_disks(f))
    if eps not in groups:
        groups[eps] = lib.Group(eps)
    seg = groups[eps].answer(f)
    out[re.search(r"S\d+E\d+", f).group(0)] = (
        None if seg is None else [round(seg.start_s, 1), round(seg.end_s, 1), seg.support]
    )
print(json.dumps(dict(sorted(out.items()))))
lib.save()
