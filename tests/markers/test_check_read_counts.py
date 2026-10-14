"""Reads a check makes for a file that is already decided: upper bounds so slow reads can't creep back.

Everything in the season is decided and published, nothing is due: the check still reads its facts (``markers.db``,
the folders, the file), but a season's shared facts are read once for the season rather than once per episode.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from collections import Counter

import pytest

from media_preview_generator.markers.pipeline import SEASON_CACHE_TTL_S
from media_preview_generator.markers.season_cache import SeasonCache
from tests.markers.audio import test_season
from tests.markers.audio.helpers import INTRO, N_POINTS, Audio, noise
from tests.markers.fakes import ready_publisher
from tests.markers.pipeline_helpers import _run
from tests.markers.test_season_followups import _season_ctx

show = test_season.show
EPISODES = 10

# Per checked file, on a steady season of EPISODES episodes, with the season already read by an earlier episode (warm)
# and with the first episode reading it (cold: its reads are shared by the EPISODES files). Before the season was
# shared they were 156 statements, 24 stats and 3 listings for every file, and grew with the season's size.
MAX_QUERIES_PER_FILE = {"warm": 40, "cold": 45}
MAX_STATS_PER_FILE = {"warm": 4, "cold": 7}  # one of them is the season folder's mtime check
MAX_LISTINGS_PER_FILE = {"warm": 0, "cold": 0.5}


class _Reads:
    """Counts the statements ``markers.db`` runs and the stat/scandir/listdir calls under a folder."""

    def __init__(self, store, root: str, monkeypatch) -> None:
        self.counts: Counter[str] = Counter()
        self.statements: Counter[str] = Counter()
        self._guard = threading.Lock()
        self._root = root
        store._conn.set_trace_callback(self._statement)
        reading = store._reading

        @contextlib.contextmanager
        def traced_reading(**kwargs):
            # Reads run on pooled connections of their own (``MarkerStore._reading``), not on the writing one.
            with reading(**kwargs) as conn:
                conn.set_trace_callback(self._statement)
                yield conn

        monkeypatch.setattr(store, "_reading", traced_reading)
        for name in ("stat", "scandir", "listdir"):
            monkeypatch.setattr(os, name, self._counting(name, getattr(os, name)))

    def _statement(self, sql: str) -> None:
        if sql.split(None, 1)[0].upper() in ("BEGIN", "COMMIT", "ROLLBACK"):
            return  # transaction bounds, not reads
        self._add("queries")
        with self._guard:
            self.statements[re.sub(r"\s+", " ", sql)[:120]] += 1

    def _add(self, name: str) -> None:
        with self._guard:
            self.counts[name] += 1

    def _counting(self, name: str, real):
        label = "stats" if name == "stat" else "listings"

        def wrapper(path=None, *args, **kwargs):
            if isinstance(path, str) and path.startswith(self._root):
                self._add(label)
            return real(path, *args, **kwargs) if path is not None else real(*args, **kwargs)

        return wrapper


def _points(path: str):
    """Every episode carries the same opening at its own offset."""
    episode = int(re.search(r"E(\d+)", os.path.basename(path)).group(1))
    body = noise(episode, N_POINTS)
    body[60 + 10 * episode : 300 + 10 * episode] = INTRO
    return body


@pytest.fixture
def steady_season(store, show):
    paths = show(1, EPISODES)
    ctx = _season_ctx(store, paths[0])
    ctx.seasons = SeasonCache(ttl_s=SEASON_CACHE_TTL_S)
    publishers = {"plex-1": ready_publisher()}
    with Audio(points=_points):
        for path in paths:  # the first pass fingerprints, matches and publishes
            if _run(ctx, path, publishers)[0] is None:
                _run(ctx, path, publishers, stage="process")
        # The second pass settles anything the first left for the season's later episodes.
        settled = [_run(ctx, path, publishers)[0].outcome_key for path in paths]
        yield ctx, paths, publishers, settled


@pytest.mark.parametrize("season", ["warm", "cold"])
def test_a_decided_file_costs_a_bounded_number_of_reads(season, steady_season, store, tmp_path, monkeypatch):
    ctx, paths, publishers, settled = steady_season
    if season == "cold":
        ctx.seasons = SeasonCache(ttl_s=SEASON_CACHE_TTL_S)
    reads = _Reads(store, str(tmp_path), monkeypatch)
    outcomes = []
    for path in paths:
        out, _ = _run(ctx, path, publishers)
        outcomes.append(out.outcome_key)
    per_file = {name: count / len(paths) for name, count in reads.counts.items()}
    print(f"\nreads per decided file: {per_file}")
    assert outcomes == settled
    assert per_file["queries"] <= MAX_QUERIES_PER_FILE[season]
    assert per_file["stats"] <= MAX_STATS_PER_FILE[season]
    assert per_file.get("listings", 0) <= MAX_LISTINGS_PER_FILE[season]
