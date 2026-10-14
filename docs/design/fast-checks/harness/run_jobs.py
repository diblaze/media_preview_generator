#!/usr/bin/env python3
"""Run the fixed job sequence against a running app and record how each job went.

Usage: run_jobs.py OUT_JSON    (env: FASTCHECK_APP_URL, FASTCHECK_TOKEN)

Order: Intro & Credits (tv), loudness (tv + movies), previews (tv + movies). Each job runs to a terminal state before
the next starts. FASTCHECK_JOBS=loudness,previews runs only those. The token is read from the environment and never printed or written.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

URL = os.environ["FASTCHECK_APP_URL"].rstrip("/")
TOKEN = os.environ["FASTCHECK_TOKEN"]
SERVER = "mlab-plex"
TV = {"server_id": SERVER, "library_id": "1"}
MOVIES = {"server_id": SERVER, "library_id": "2"}
TERMINAL = {"completed", "failed", "cancelled"}
JOB_TIMEOUT_S = int(os.environ.get("FASTCHECK_JOB_TIMEOUT_S", str(10 * 3600)))

JOBS = [
    ("intro_credits", "POST", "/api/markers/jobs", {"libraries": [TV], "priority": "high"}),
    ("loudness", "POST", "/api/loudness/jobs", {"libraries": [TV, MOVIES], "priority": "high"}),
    ("previews", "POST", "/api/jobs", {"libraries": [TV, MOVIES], "priority": 1}),
]


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(URL + path, data=data, method=method)
    request.add_header("X-Auth-Token", TOKEN)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310 - the lab app URL, http only
        return json.load(response)


def wait_healthy() -> None:
    for _ in range(180):
        try:
            with urllib.request.urlopen(URL + "/api/health", timeout=5) as response:  # nosec B310 - the lab app URL, http only
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    raise SystemExit("app never became healthy")


def seconds(job: dict) -> float | None:
    if not job.get("started_at") or not job.get("completed_at"):
        return None
    return (datetime.fromisoformat(job["completed_at"]) - datetime.fromisoformat(job["started_at"])).total_seconds()


def wait_job(job_id: str) -> dict:
    deadline = time.time() + JOB_TIMEOUT_S
    while time.time() < deadline:
        job = call("GET", f"/api/jobs/{job_id}")
        if job["status"] in TERMINAL:
            return job
        time.sleep(3)
    raise SystemExit(f"job {job_id} did not finish in {JOB_TIMEOUT_S}s")


def settle() -> None:
    """Wait until nothing is queued or running (retry jobs a failed file spawns count)."""
    quiet_since = None
    for _ in range(600):
        active = call("GET", "/api/jobs?status=active&per_page=200").get("jobs", [])
        if active:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since > 8:
            return
        time.sleep(2)


def main() -> None:
    wait_healthy()
    results = []
    only = set(filter(None, os.environ.get("FASTCHECK_JOBS", "").split(",")))
    for name, method, path, body in JOBS:
        if only and name not in only:
            continue
        started = time.time()
        created = call(method, path, body)
        ids = created.get("jobs") or [created["id"]]
        jobs = [wait_job(job_id) for job_id in ids]
        settle()
        wall = time.time() - started
        entry = {
            "name": name,
            "ids": ids,
            "wall_s": round(wall, 2),
            "job_s": [seconds(job) for job in jobs],
            "status": [job["status"] for job in jobs],
            "error": [job.get("error") for job in jobs],
            "progress": [
                {k: job["progress"].get(k) for k in ("total_items", "processed_items", "outcome", "marker_sources")}
                for job in jobs
            ],
        }
        results.append(entry)
        print(f"{name}: {entry['status']} in {wall:.0f}s, {entry['progress']}", file=sys.stderr, flush=True)
    with open(sys.argv[1], "w") as handle:
        json.dump(results, handle, indent=1)


if __name__ == "__main__":
    main()
