#!/usr/bin/env python3
"""Turn a copy of the lab app's settings.json into the harness's fixed settings.

Usage: prep_settings.py IN_SETTINGS OUT_SETTINGS WORKERS

Only the copy is changed. Tokens stay inside the file; nothing is printed.

- Lab Plex stays enabled with its tv (1) and movies (2) libraries; every other server and library is disabled.
- Plex chapter thumbnails, Intro & Credits (intro + credits) and loudness are switched on.
- The three online sources (TheIntroDB, IntroDB, SkipDB) are switched off: their answers depend on the internet and
  a daily budget, so two runs could differ for reasons that have nothing to do with the code under test.
- Workers: CPU only, WORKERS of them, all job types, always available. No GPU, so both images decide the same way.
"""

from __future__ import annotations

import json
import sys

PLEX_ID = "mlab-plex"
LIBRARIES = {"1", "2"}
ONLINE_SOURCES = {"theintrodb", "introdb", "skipdb"}


def main() -> None:
    src, dst, workers = sys.argv[1], sys.argv[2], int(sys.argv[3])
    with open(src) as handle:
        settings = json.load(handle)

    for server in settings["media_servers"]:
        if server["id"] != PLEX_ID:
            server["enabled"] = False
            continue
        server["enabled"] = True
        for library in server["libraries"]:
            library["enabled"] = library["id"] in LIBRARIES
        server.setdefault("output", {})["chapter_thumbnails"] = True
        server.setdefault("markers", {})["enabled"] = True
        server["markers"]["library_ids"] = None
        server["loudness"] = {"enabled": True, "library_ids": None}

    markers = settings.setdefault("markers", {})
    markers["detect"] = {"intro": True, "credits": True, "recap": False}
    for source in markers.get("sources", []):
        if source["id"] in ONLINE_SOURCES:
            source["enabled"] = False

    settings["gpu_config"] = []
    settings["cpu_threads"] = workers
    settings["worker_groups"] = [
        {
            "id": "fastcheck-cpu",
            "name": "Fast-check CPU",
            "enabled": True,
            "availability": {"mode": "always", "windows": []},
            "members": [
                {
                    "id": "m1",
                    "resource": "cpu",
                    "device": None,
                    "count": workers,
                    "job_types": ["previews", "intro_credits", "loudness"],
                }
            ],
        }
    ]
    settings["worker_groups_revision"] = 0

    with open(dst, "w") as handle:
        json.dump(settings, handle, indent=1)
    import os

    os.chmod(dst, 0o600)


if __name__ == "__main__":
    main()
