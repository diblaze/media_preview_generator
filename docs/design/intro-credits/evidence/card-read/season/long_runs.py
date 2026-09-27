"""Scratch: pair runs by length in the raw pair cache (new window only: point counts past 900 s), and the longest."""

import os
import pickle
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
pairs = pickle.load(open(os.path.join(HERE, "pairs_raw.pkl"), "rb"))
POINT_S = 0.1238
bins = Counter()
long = []
new_window = 0
for key, runs in pairs.items():
    a, b, _fa, _fb, na, nb = key
    if max(na, nb) * POINT_S <= 905:  # the old window's pairs
        continue
    new_window += 1
    for r in runs:
        length = r[1] - r[0]
        bins["<=120" if length <= 120 else "120-300" if length <= 300 else ">300"] += 1
        if length > 120:
            long.append((round(length), round(r[0]), round(r[2]), os.path.basename(a)[:45], os.path.basename(b)[:45]))
print("new-window pairs", new_window, dict(bins))
for row in sorted(long, reverse=True)[:25]:
    print(row)
