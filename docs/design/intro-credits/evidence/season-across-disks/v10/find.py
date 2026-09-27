"""Scratch: find an episode of a show/season across the roots. find_episode(show_substring, season)"""

import os
import re

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]


def find_episode(show: str, season: int, own_root: str | None = None) -> str:
    for root in ROOTS if own_root is None else [own_root]:
        base = f"{root}/TV Shows"
        for name in sorted(os.listdir(base)):
            if show.lower() not in name.lower():
                continue
            for sname in sorted(os.listdir(os.path.join(base, name))):
                m = re.match(r"^season\s*(\d+)$", sname, re.I)
                if m and int(m.group(1)) == season:
                    folder = os.path.join(base, name, sname)
                    vids = sorted(f for f in os.listdir(folder) if f.endswith((".mkv", ".mp4", ".avi", ".m4v", ".ts")))
                    if vids:
                        return os.path.join(folder, vids[0])
    raise FileNotFoundError(show)
