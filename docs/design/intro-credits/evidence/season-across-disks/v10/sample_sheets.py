"""Scratch: contact sheets for the sample's answers (after, and before when it differs by more than 0.5 s)."""

import json
import os
import re

import lib
from sheet import sheet

rows = json.load(open(lib.HERE / "sample_app.json"))
for i, row in enumerate(rows):
    answers = []
    if row["after"]:
        answers.append(("after", row["after"][0], row["after"][1]))
    b = row["before"]
    if b and (not row["after"] or abs(b[0] - row["after"][0]) > 0.5 or abs(b[1] - row["after"][1]) > 0.5):
        answers.append(("before", b[0], b[1]))
    if not answers:
        continue
    code = re.search(r"S\d+E\d+", row["file"]).group(0)
    show = os.path.basename(os.path.dirname(os.path.dirname(row["file"]))).split(" (")[0].replace(" ", "_")
    out = str(lib.HERE / "sheets" / f"{i:02d}_{show}_{code}.jpg")
    sheet(row["file"], answers, out)
    print(out, answers, flush=True)
