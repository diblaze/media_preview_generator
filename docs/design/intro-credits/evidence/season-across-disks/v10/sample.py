"""Scratch: fixed-seed sample of split seasons (split.json) and 3 episodes each. Writes sample.json."""

import json
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov", ".mpg", ".mpeg", ".webm", ".m2ts")
split = json.load(open(os.path.join(HERE, "split.json")))
rng = random.Random(20260927)
seasons = rng.sample(split, 14)
out = []
for s in seasons:
    files = sorted(
        os.path.join(folder, f) for folder in s["folders"] for f in os.listdir(folder) if f.lower().endswith(VIDEO)
    )
    picked = sorted(rng.sample(files, min(3, len(files))))
    out.append({"show": s["show"], "season": s["season"], "folders": s["folders"], "episodes": picked})
    print(s["show"], s["season"], {f.split("/")[1]: n for f, n in s["folders"].items()}, len(files))
json.dump(out, open(os.path.join(HERE, "sample.json"), "w"), indent=1)
