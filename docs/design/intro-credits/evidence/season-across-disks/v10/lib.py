"""Scratch: run a code tree's season step offline on local media (read-only), with disk caches.

CODE env var picks the tree (the dev baseline export or the worktree). Fingerprints come from the eval cache
(~/.cache/markers_eval, CPU ffmpeg); pair runs and end-picture shares are cached in this folder (pair runs don't depend on
the selection rules, so both trees share them).
"""

from __future__ import annotations

import collections
import os
import pickle
import re
import sys
import threading
from pathlib import Path

HERE = Path(__file__).parent
CODE = os.environ.get("CODE", str(HERE / "base"))
sys.path.insert(0, CODE)

import numpy as np  # noqa: E402

import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio import matcher as M  # noqa: E402
from media_preview_generator.markers.speed import playback_speed  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

assert S.__file__.startswith(CODE), S.__file__

OLD = Path(
    "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick/data"
)
ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
DECODE = os.environ.get("DECODE", "gpu")
reader = (
    end_picture.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
    if DECODE == "gpu"
    else end_picture.Reader(ffmpeg="/usr/bin/ffmpeg")
)
_lock = threading.Lock()


def _load(name, default):
    p = HERE / name
    if p.exists():
        with open(p, "rb") as fh:
            return pickle.load(fh)
    return default


PAIRS: dict = _load("pairs.pkl", {})
SHARES: dict = _load("shares.pkl", {})
RATES: dict = {}
UNREADABLE: set[str] = set()


def save():
    with _lock:
        for name, obj in (("pairs.pkl", PAIRS), ("shares.pkl", SHARES)):
            tmp = HERE / (name + ".tmp")
            with open(tmp, "wb") as fh:
                pickle.dump(obj, fh)
            os.replace(tmp, HERE / name)


# ---------------------------------------------------------------- truth sets


def load_truth(mode: str) -> dict[str, tuple | None]:
    if mode == "lists":
        d = pickle.load(open(OLD / "eval.pkl", "rb"))
        return {r["file"]: tuple(r["truth"]) for r in d["truth"] if r["truth"]}
    if mode == "scale_clean":
        d = pickle.load(open(OLD / "scale.pkl", "rb"))
        out = {}
        for season, files in d["seasons"].items():
            if "Ninja Kamui" in season or "Law and Order Criminal Intent" in season:
                continue
            for f in files:
                if f in d["fps"] and d["truth"].get(f):
                    out[f] = tuple(d["truth"][f])
        return out
    if mode == "chap_clean":
        d = pickle.load(open(OLD / "chap.pkl", "rb"))
        out = {}
        for season, files in d["seasons"].items():
            if any(
                x in season
                for x in ("Blood Legacy (2024)", "Community (2009) {tvdb-94571}/Season 04", "Dragon Striker (2026)")
            ):
                continue
            for f in files:
                t = d["truth"].get(f)
                if f in d["fps"] and t and t[1] - t[0] <= 180:
                    out[f] = tuple(t)
        return out
    if mode == "accused":
        t = pickle.load(open(OLD / "accused_truth.pkl", "rb"))
        return {f: (tuple(v) if v else None) for f, v in t.items()}
    raise ValueError(mode)


def judge(seg, truth) -> str:
    if truth is None:
        return "wrong" if seg else "none-ok"
    if seg is None:
        return "missed"
    return "useful" if abs(seg[1] - truth[1]) <= 5 and abs(seg[0] - truth[0]) <= 15 else "wrong"


# ---------------------------------------------------------------- media


def points(path: str) -> np.ndarray | None:
    if path in UNREADABLE:
        return None
    try:
        found = cache.points(path)
    except Exception as exc:  # noqa: BLE001 - left out, as the app leaves out a member ffmpeg can't read
        print("   unreadable", os.path.basename(path)[:70], type(exc).__name__, flush=True)
        UNREADABLE.add(path)
        return None
    return found


def rate(path: str):
    if path not in RATES:
        try:
            RATES[path] = cache.frame_rate(path)
        except Exception:  # noqa: BLE001
            RATES[path] = None
    return RATES[path]


def pair(a_path, b_path, a_pts, b_pts, fa, fb):
    key = (a_path, b_path, None if fa is None else round(fa, 6), None if fb is None else round(fb, 6))
    if key not in PAIRS:
        PAIRS[key] = [tuple(r) for r in S.season_pair_runs(a_pts, b_pts)]
    return [M.Run(*r) for r in PAIRS[key]]


def share(target, partner, start_s, end_s, offset_s):
    key = (target, partner, round(start_s * 1000), round(end_s * 1000), round(offset_s * 1000))
    if key not in SHARES:
        try:
            SHARES[key] = reader.share(target, partner, start_s, end_s, offset_s)
        except end_picture.ReadFailedError as exc:
            print("   end picture read failed", exc, flush=True)
            SHARES[key] = "failed"
    return SHARES[key]


class Group:
    """One season group's matching state, as the app builds it (``season._matching``) for the group's files."""

    def __init__(self, episodes):
        fps = {f: p for f in episodes if (p := points(f)) is not None}
        self.own = fps
        audible = {f: p for f, p in fps.items() if len(p)}
        by_rate = S.season_clock({f: playback_speed(rate(f)) for f in audible})
        stretched = {f: cache.retimed(f, factor) for f, factor in by_rate.factors.items()}

        def heard(path, retimed_side, reference):
            own = stretched[path] if retimed_side else audible[path]
            first = path < reference
            fa = by_rate.factors[path] if retimed_side else None
            if first:
                runs = pair(path, reference, own, audible[reference], fa, None)
            else:
                runs = pair(reference, path, audible[reference], own, None, fa)
            return S.heard_in(runs, own, first=first)

        refs = [f for f in audible if playback_speed(rate(f)) == by_rate.speed]
        self.clock = S.clock_by_audio(S.SeasonClock(by_rate.speed, dict(by_rate.factors)), refs, heard)
        self.points = dict(audible)
        self.points.update({f: stretched[f] for f in self.clock.factors})
        self.files = sorted(self.points)

    def runs_between(self, a, b):
        f = self.clock.factors
        return pair(a, b, self.points[a], self.points[b], f.get(a), f.get(b))

    def end_picture_passes(self, target):
        def passes(candidate):
            c = S.in_own_times(candidate, target, self.clock.factors)
            got = []
            for hit in end_picture.partners([h for h in c.members if os.path.exists(h.partner)]):
                s = share(target, hit.partner, c.segment.start_s, c.segment.end_s, hit.partner_start_s - hit.start_s)
                if s == "failed":
                    continue
                got.append(s)
            return end_picture.passes(got)

        return passes

    def answer(self, target):
        """The season step's answer for one episode, in its own seconds (None: none)."""
        if target not in self.files or len(self.files) < 2:
            return None
        seg = S.season_intro(
            target, self.files, self.points, self.runs_between, end_picture_passes=self.end_picture_passes(target)
        )
        return None if seg is None else S.in_own_time(seg, self.clock.factors.get(target))


# ---------------------------------------------------------------- grouping (scratch, before the app has one)

_SEASON_DIR = re.compile(r"^(?:season|series|staffel|saison)\s*(\d{1,4})$", re.IGNORECASE)
_TVDB = re.compile(r"\{tvdb-(\d+)\}")


def across_disks(path: str) -> tuple[str, ...]:
    """Scratch cross-disk group: same show folder (by tvdb id, else name) under every TV root, same season number."""
    folder = os.path.dirname(path)
    show = os.path.dirname(folder)
    if not _SEASON_DIR.match(os.path.basename(folder)):
        return S.season_group(path).episodes
    name = os.path.basename(show)
    tv = _TVDB.search(name)
    videos = []
    for root in ROOTS:
        base = f"{root}/TV Shows"
        try:
            shows = os.listdir(base)
        except OSError:
            continue
        for other in shows:
            m = _TVDB.search(other)
            if not ((tv and m and m.group(1) == tv.group(1)) or other == name):
                continue
            for season in os.listdir(os.path.join(base, other)):
                sm = _SEASON_DIR.match(season)
                if sm and int(sm.group(1)) == int(_SEASON_DIR.match(os.path.basename(folder)).group(1)):
                    videos.extend(S.folder_videos(os.path.join(base, other, season)))
    return S.season_group(path, videos).episodes


def tally(rows):
    c = collections.Counter(rows)
    return f"{c['useful']:3} / {c['wrong']:3} / {c['missed']:3}" + (
        f" (none-ok {c['none-ok']})" if c["none-ok"] else ""
    )
