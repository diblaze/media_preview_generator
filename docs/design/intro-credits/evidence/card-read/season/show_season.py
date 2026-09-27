"""Scratch: every row of a season in two season runs (pickles), by a name filter. Usage: show_season.py A.pkl B.pkl filter"""

import os
import pickle
import sys

a = pickle.load(open(sys.argv[1], "rb"))
b = pickle.load(open(sys.argv[2], "rb"))
for key in sorted(set(a) | set(b)):
    mode, f = key
    if sys.argv[3] not in f:
        continue
    ra, rb = a.get(key), b.get(key)
    print(mode, os.path.basename(f)[:60], "truth", (ra or rb)["truth"], "|", ra and (ra["answer"], ra["verdict"]), "->",
          rb and (rb["answer"], rb["verdict"]))  # fmt: skip
