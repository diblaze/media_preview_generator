import cProfile
import json
import pstats
import time

import lib
from app_group import LIBRARY
from lib import S

fs = [e for s in json.load(open(lib.HERE / "sample.json")) for e in s["episodes"]]
t0 = time.time()
for f in fs:
    S.season_folders(f, [LIBRARY])
print(f"season_folders {(time.time() - t0) / len(fs) * 1000:.1f} ms per call")
t0 = time.time()
for f in fs:
    S.season_videos(f, [LIBRARY])
print(f"season_videos {(time.time() - t0) / len(fs) * 1000:.1f} ms per call")
cProfile.run("for f in fs[:10]: S.season_folders(f, [LIBRARY])", str(lib.HERE / "prof.out"))
pstats.Stats(str(lib.HERE / "prof.out")).sort_stats("tottime").print_stats(8)
