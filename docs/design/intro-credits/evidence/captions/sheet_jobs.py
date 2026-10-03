"""Sheet jobs for files whose credit text changed between two ct runs: the base start, the work start (if any) and the
file's last 150 s. Usage: mkjobs.py base.json work.json out_jobs.json [--prefix C] [--only substr,...]"""

import json
import os
import sys

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
prefix = sys.argv[sys.argv.index("--prefix") + 1] if "--prefix" in sys.argv else "C"
only = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
jobs = []
n = 0
for path in sorted(set(base) & set(work)):
    b, w = base[path], work[path]
    if "error" in b or "error" in w or b.get("start_s") == w.get("start_s"):
        continue
    if only and not any(s in path for s in only):
        continue
    n += 1
    dur = b["duration"] / 1000
    rows = []
    for label, s in (("base", b.get("start_s")), ("work", w.get("start_s"))):
        if s is not None:
            rows.append({"label": f"{label} {s:.1f} -10..+30 every 2 s", "center": s, "before": 10, "after": 30,
                         "step": 2.0})  # fmt: skip
    rows.append({"label": "last 150 s every 5 s", "center": dur - 150, "before": 0, "after": 150, "step": 5.0})
    jobs.append({"id": f"{prefix}{n:02d}", "title": f"{prefix}{n:02d} {os.path.basename(path)[:70]} base "
                 f"{b.get('start_s')} work {w.get('start_s')} end {dur:.0f}", "path": path, "rows": rows,
                 "keys": {}})  # fmt: skip
json.dump(jobs, open(sys.argv[3], "w"), indent=1)
print(len(jobs), "jobs")
