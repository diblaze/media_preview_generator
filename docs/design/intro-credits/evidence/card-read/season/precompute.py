"""Scratch: fingerprint every member of every group the season runs will read, a few at a time (CODE's window).

Usage: CODE=... precompute.py N mode... [sample]
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import slib

workers, modes = int(sys.argv[1]), sys.argv[2:]
members: dict[str, None] = {}
for mode in modes:
    if mode == "sample":
        files = [f for s in json.load(open(slib.HERE / "sample.json")) for f in s["episodes"]]
    else:
        files = [f for f in slib.load_truth(mode) if os.path.exists(f)]
    for f in files:
        for m in slib.app_group(f):
            members.setdefault(m, None)
todo = list(members)
print(f"{len(todo)} members", flush=True)


def one(path: str) -> tuple[str, float, str]:
    t0 = time.time()
    try:
        slib.cache.points(path)
        return path, time.time() - t0, ""
    except Exception as exc:  # noqa: BLE001
        return path, time.time() - t0, f"{type(exc).__name__}: {exc}"[:120]


t0 = time.time()
done = 0
with ThreadPoolExecutor(workers) as pool:
    for fut in as_completed([pool.submit(one, p) for p in todo]):
        path, took, err = fut.result()
        done += 1
        if err or done % 50 == 0:
            print(f"{done}/{len(todo)} {took:5.1f}s {os.path.basename(path)[:60]} {err}", flush=True)
print(f"done in {time.time() - t0:.0f}s", flush=True)
