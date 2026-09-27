"""Scratch: the season step's intro for every intro item of the audit's answer key, in one tree (CODE), as replay.py's
--audio JSON: {path: [start_s, end_s] | null}.

Usage: CODE=... audit_audio.py out.json
"""

import json
import sys

import slib

items = json.load(open(slib.HERE / "ct" / "items.json"))
paths = sorted({it["path"] for it in items["verdict"] + items["plex"] if it["type"] == "intro" and it.get("on_disk")})
groups: dict[tuple, slib.Group] = {}
out = {}
for n, path in enumerate(paths, 1):
    eps = tuple(slib.app_group(path))
    if eps not in groups:
        groups[eps] = slib.Group(eps)
    seg = groups[eps].answer(path)
    out[path] = None if seg is None else [seg.start_s, seg.end_s]
    print(n, len(paths), out[path], path.rsplit("/", 1)[-1][:60], flush=True)
    slib.save()
json.dump(out, open(sys.argv[1], "w"), indent=0)
print("DONE", len(out))
