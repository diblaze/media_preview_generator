"""Scratch: contact sheets for set files whose verdict changed (answer and truth). Usage: set_sheets.py substring..."""

import os
import pickle
import re
import sys

import lib
from sheet import sheet

base = pickle.load(open(lib.HERE / "sets_base.pkl", "rb"))
app = pickle.load(open(lib.HERE / "sets_app.pkl", "rb"))
for (mode, f), row in app.items():
    if not any(s in f for s in sys.argv[1:]):
        continue
    before = base[(mode, f)]["own"]
    after = row["across"]
    answers = []
    if after:
        answers.append(("after", after[0], after[1]))
    if before and before != after:
        answers.append(("before", before[0], before[1]))
    if row["truth"]:
        answers.append(("truth", float(row["truth"][0]), float(row["truth"][1])))
    code = re.search(r"S\d+E\d+|\d{4}-\d\d-\d\d", f).group(0)
    show = os.path.basename(os.path.dirname(os.path.dirname(f))).split(" (")[0].replace(" ", "_")
    out = str(lib.HERE / "sheets" / f"set_{show}_{code}.jpg")
    sheet(f, answers, out)
    print(out, answers, flush=True)
