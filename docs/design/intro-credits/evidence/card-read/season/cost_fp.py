"""Scratch: CPU cost per episode of the season fingerprint and of one pair's matching, old window vs new.

Old: min(900 s, 35 %). New: min(1200 s, 50 %). Files by name substrings from season_base.pkl (read-only on media).
Usage: cost_fp.py "<a substring>|<b substring>" ...   (a and b: two episodes of one season)
"""

import os
import pickle
import resource
import subprocess
import sys
import time

import numpy as np

WORK = "/home/data/workspace/plex_generate_vid_previews"
sys.path.insert(0, WORK)
from media_preview_generator.markers.audio import fingerprint as fp  # noqa: E402
from media_preview_generator.markers.audio import matcher as M  # noqa: E402
from media_preview_generator.markers.probe import probe_media  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
keys = [k[1] for k in pickle.load(open(os.path.join(HERE, "season_base.pkl"), "rb"))]
ffmpeg = fp.chromaprint_ffmpeg(None)
ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe")


def find(parts: str) -> str:
    wanted = parts.split("&")
    return next(k for k in sorted(set(keys)) if all(w in os.path.basename(k) for w in wanted))


def fingerprint(path: str, length_s: float) -> tuple[np.ndarray, float]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    out = subprocess.run(["nice", "-n", "19", *fp.fingerprint_command(ffmpeg, path, length_s)], capture_output=True,
                         check=True).stdout  # fmt: skip
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)
    return np.frombuffer(out, dtype="<u4").astype(np.uint32), cpu


for pair in sys.argv[1:]:
    a, b = (find(p) for p in pair.split("|"))
    durations = [probe_media(p, ffprobe=ffprobe).duration_ms for p in (a, b)]
    print("==", os.path.basename(a)[:50], "|", os.path.basename(b)[:50], [round(d / 1000) for d in durations])
    for label, cap, frac in (("old", 900.0, 0.35), ("new", 1200.0, 0.5)):
        windows = [min(cap, frac * d / 1000) for d in durations]
        (pa, ca), (pb, cb) = (fingerprint(p, w) for p, w in zip((a, b), windows, strict=True))
        t = time.process_time()
        runs = M.pair_runs(pa, pb)
        match = time.process_time() - t
        print(f"   {label}: windows {windows[0]:.0f}/{windows[1]:.0f} s  fingerprint CPU {ca:.2f}/{cb:.2f} s  "
              f"one pair's matching {match:.2f} s  runs {len(runs)}")  # fmt: skip
