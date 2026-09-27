"""Scratch: selection-rule variants on lib.Group (base tree's matcher and guards), for trying rules before porting.

pick(g, target, flags) mirrors season.season_intro with optional rules:
  W   pass over a cluster whose end reaches the end of the target's fingerprint (window cut)
  B   a cluster starting in the first 2 s yields to a later quorum cluster that floats (passes the guards)
  Q   a quorum per opening (camps)
"""

from __future__ import annotations

import os

import numpy as np

import lib
from lib import M, S

POINT_S = 0.1238
FLOAT_S = 3.0


def floats(c) -> bool:
    return sum(abs(h.partner_start_s - h.start_s) > FLOAT_S for h in c.members) * 2 > len(c.members)


def cut_by_window(g, target, c) -> bool:
    last = (len(g.points[target]) - 1) * POINT_S
    return c.segment.end_s >= last - M.MAX_GAP_S


def _best_within(g, y, pool, memo):
    key = (y, frozenset(pool))
    if key not in memo:
        hits = [h for h in M.file_hits(y, g.files, g.runs_between) if h.partner in pool]
        cands = M.intro_candidates(hits)
        memo[key] = cands[0] if cands else None
    return memo[key]


def _opening(c, pool_size) -> bool:
    return (
        c is not None
        and c.segment.end_s - c.segment.start_s >= M.PREFERRED_MIN_S
        and M.meets_quorum(c.segment.support, pool_size)
    )


def opening_quorum(g, target, c, memo, min_support=1, contiguous=False) -> bool:
    """Whether a cluster below the season's quorum has the quorum of its own opening."""
    others = len(g.files) - 1
    if c.segment.support < min_support:
        return False
    partners = {h.partner for h in c.members}
    rest = [f for f in g.files if f != target and f not in partners]
    rest_set = set(rest)
    opened = {y for y in rest if _opening(_best_within(g, y, rest_set - {y}, memo), len(rest) - 1)}
    if not opened:
        return False
    for p in partners:
        if _opening(_best_within(g, p, opened - {p}, memo), len(opened)):
            return False  # the stretch's supporters share the other opening: it isn't one of its own
    if contiguous:
        episode = {f: S._folder_video(f).episode for f in g.files}
        mine = [episode[f] for f in partners | {target}]
        theirs = [episode[f] for f in opened]
        if None in mine or None in theirs:
            return False
        if not (max(mine) < min(theirs) or min(mine) > max(theirs)):
            return False
    return M.meets_quorum(c.segment.support, others - len(opened))


def pick(g: lib.Group, target: str, flags: str = "", memo=None, min_support=1):
    memo = {} if memo is None else memo
    if target not in g.files or len(g.files) < 2:
        return None
    files, points = g.files, g.points
    others = len(files) - 1
    passes = g.end_picture_passes(target)
    cands = M.intro_candidates(M.file_hits(target, files, g.runs_between))

    def quorum(c):
        if M.meets_quorum(c.segment.support, others):
            return True
        if "Q" not in flags:
            return False
        if "L" in flags and c.segment.end_s - c.segment.start_s < M.PREFERRED_MIN_S:
            return False
        return opening_quorum(g, target, c, memo, min_support, contiguous="C" in flags)

    def ok(c):
        if "W" in flags and cut_by_window(g, target, c):
            return False
        return S._passes_guards(target, c, points, passes)

    chosen = None
    for c in cands:
        if not quorum(c):
            break
        if ok(c):
            chosen = c
            break
    if chosen is not None and "B" in flags and chosen.segment.start_s < S.FILE_START_S:
        for c in cands:
            if c.segment.start_s >= chosen.segment.end_s and ("F" in flags or floats(c)) and quorum(c) and ok(c):
                chosen = c
                break
    if chosen is None:
        return None
    seg = chosen.segment
    if S._mostly_silence(points[target], seg):
        return None
    return S.in_own_time(seg, g.clock.factors.get(target))
