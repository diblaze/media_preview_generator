"""Scratch: sheets for a season's app answers. Usage: season_sheets.py show season code,code,..."""

import re
import sys

import lib
from find import find_episode
from sheet import sheet

path = find_episode(sys.argv[1], int(sys.argv[2]))
g = lib.Group(lib.across_disks(path))
for code in sys.argv[3].split(","):
    f = next(x for x in g.files if code in x)
    seg = g.answer(f)
    if seg is None:
        print(code, "none")
        continue
    out = str(lib.HERE / "sheets" / f"season_{re.sub(r'[^A-Za-z]', '', sys.argv[1])}_{code}.jpg")
    sheet(f, [("answer", seg.start_s, seg.end_s)], out)
    print(out, round(seg.start_s, 1), round(seg.end_s, 1), seg.support, flush=True)
lib.save()
