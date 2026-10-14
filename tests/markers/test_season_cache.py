"""What a job shares between a season's episodes (``season_cache``), the store's read memo and its no-op upsert."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from media_preview_generator.markers.audio import season
from media_preview_generator.markers.models import FileIdentity, Source
from media_preview_generator.markers.pipeline import SEASON_CACHE_TTL_S
from media_preview_generator.markers.season_cache import SeasonCache
from media_preview_generator.markers.store import MarkerStore
from tests.markers.audio import test_season
from tests.markers.audio.helpers import Audio
from tests.markers.fakes import ready_publisher
from tests.markers.pipeline_helpers import _run
from tests.markers.test_season_followups import _season_ctx

show = test_season.show


class TestSeasonCache:
    def test_nothing_is_kept_without_a_ttl(self):
        cache = SeasonCache()
        assert cache.entry("/s1") is not cache.entry("/s1")

    def test_an_entry_is_reused_until_its_time_is_up(self):
        now = [0.0]
        cache = SeasonCache(ttl_s=60.0, clock=lambda: now[0])
        first = cache.entry("/s1")
        now[0] = 59.9
        assert cache.entry("/s1") is first
        now[0] = 60.0
        assert cache.entry("/s1") is not first

    def test_a_forgotten_entry_is_read_again_but_a_newer_one_stays(self):
        now = [0.0]
        cache = SeasonCache(ttl_s=60.0, clock=lambda: now[0])
        old = cache.entry("/s1")
        now[0] = 61.0
        newer = cache.entry("/s1")
        cache.forget("/s1", old)
        assert cache.entry("/s1") is newer
        cache.forget("/s1", newer)
        assert cache.entry("/s1") is not newer

    def test_threads_asking_together_share_one_build_and_see_it_finished(self):
        entry = SeasonCache(ttl_s=60.0).entry("/s1")
        builds, seen = [], []
        started = threading.Barrier(8)

        def build():
            builds.append(1)
            threading.Event().wait(0.05)  # a slow listing: every other thread is waiting for it by now
            return ("videos", "complete")

        def ask():
            started.wait()
            seen.append(entry.once("videos", build))

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(builds) == 1 and seen == [("videos", "complete")] * 8

    def test_results_start_empty_once_the_stores_generation_moves(self):
        entry = SeasonCache(ttl_s=60.0).entry("/s1")
        results = entry.results(1)
        results.signatures["a"] = "x"
        assert entry.results(1) is results
        assert entry.results(2).signatures == {}


@pytest.fixture
def cached(store, show):
    """A season of 4 checked once, with a job context that keeps what it reads of the season."""
    paths = show(1, 4)
    ctx = _season_ctx(store, paths[0])
    now = [0.0]
    ctx.seasons = SeasonCache(ttl_s=SEASON_CACHE_TTL_S, clock=lambda: now[0])
    publishers = {"plex-1": ready_publisher()}
    with Audio():
        for path in paths:
            if _run(ctx, path, publishers)[0] is None:
                _run(ctx, path, publishers, stage="process")
        yield SimpleCtx(ctx, paths, publishers, now)


class SimpleCtx:
    def __init__(self, ctx, paths, publishers, now):
        self.ctx, self.paths, self.publishers, self.now = ctx, paths, publishers, now

    def due(self, path):
        return season.season_audio_due(self.ctx.store.get_file(path), self.ctx)

    def view_of(self, path):
        with self.ctx._running(path):
            return season._season_view(self.ctx, path)


def _replace(path):
    with open(path, "ab") as f:
        f.write(b"cut")
    os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))


class TestSeasonSharedBetweenEpisodes:
    def test_a_season_is_listed_once_for_all_its_episodes(self, cached):
        with patch.object(season, "folder_videos", wraps=season.folder_videos) as listed:
            for path in cached.paths:
                cached.due(path)
        assert listed.call_count == 0  # read by the checks that built the cache, not again for these

    def test_a_sibling_replaced_on_disk_is_seen_when_the_time_is_up_not_before(self, cached):
        e1, e2 = cached.paths[:2]
        assert cached.due(e1) is False
        _replace(e2)
        assert cached.due(e1) is False  # within the time: the season is as it was listed
        cached.now[0] = SEASON_CACHE_TTL_S + 1
        assert cached.due(e1) is True

    def test_a_file_added_to_the_folder_is_seen_by_the_next_episode_with_no_time_passed(self, cached):
        e1 = cached.paths[0]
        assert cached.due(e1) is False
        folder = os.path.dirname(e1)
        with open(os.path.join(folder, "Show (2020) - S01E05.mkv"), "wb") as f:
            f.write(b"x" * 105)
        assert cached.due(e1) is True  # the folder's mtime moved: the season is listed again

    def test_a_file_removed_from_the_folder_is_seen_by_the_next_episode_with_no_time_passed(self, cached):
        e1, e4 = cached.paths[0], cached.paths[3]
        assert cached.due(e1) is False
        os.remove(e4)
        assert cached.due(e1) is True

    def test_a_folder_that_cant_be_stat_is_listed_again(self, cached):
        folder = os.path.dirname(cached.paths[0])
        listed = cached.ctx.seasons.entry(folder)
        assert cached.ctx.seasons.entry(folder) is listed
        with patch.object(os, "stat", side_effect=OSError("stale handle")):
            assert cached.ctx.seasons.entry(folder) is not listed

    def test_an_overwrite_in_place_is_seen_after_the_time_is_up(self, cached):
        e1, e2 = cached.paths[:2]
        assert cached.due(e1) is False
        _replace(e2)  # same name: the folder's mtime does not move
        assert cached.due(e1) is False
        cached.now[0] = SEASON_CACHE_TTL_S + 1
        assert cached.due(e1) is True

    def test_the_file_checked_is_always_read_as_it_is_on_disk(self, cached):
        e3 = cached.paths[2]
        _replace(e3)
        out, _ = _run(cached.ctx, e3, cached.publishers)
        assert out is None  # its fingerprints are gone with the old file: handed to a worker, with no time passed

    def test_a_write_for_a_sibling_ends_what_was_read_of_the_season(self, cached):
        e1, e2 = cached.paths[:2]
        assert cached.due(e1) is False
        sibling = cached.ctx.store.get_file(e2)
        cached.ctx.store.set_frame_rate(sibling.id, 25.0, identity=(sibling.size, sibling.mtime_ns))
        assert cached.due(e1) is True  # a frame rate read later makes the answer due, with no time passed

    def test_a_season_that_cant_be_listed_is_listed_again_by_the_next_episode(self, store, show):
        paths = show(1, 2)
        ctx = _season_ctx(store, paths[0])
        ctx.seasons = SeasonCache(ttl_s=SEASON_CACHE_TTL_S)
        real = season.folder_videos

        def unreadable(folder, unreadable=None):
            if unreadable is not None:
                unreadable.add(folder)
            return ()

        with patch.object(season, "folder_videos", side_effect=unreadable):
            assert season._SeasonView(ctx, paths[0]).unreadable
        with patch.object(season, "folder_videos", wraps=real) as listed:
            assert not season._SeasonView(ctx, paths[1]).unreadable
        assert listed.call_count == 1


class TestReadMemo:
    @pytest.fixture
    def rec(self, store, tmp_path):
        return store.upsert_file(
            FileIdentity(str(tmp_path / "a.mkv"), 1, 2), duration_ms=1000, season_key=None, is_movie=False
        )

    def test_a_read_is_made_once_inside_a_run(self, store, rec):
        with store.read_memo(), patch.object(store, "_reading", wraps=store._reading) as reading:
            store.get_evidence(rec.id)
            store.get_evidence(rec.id)
        assert reading.call_count == 1

    def test_reads_outside_a_run_are_never_remembered(self, store, rec):
        with patch.object(store, "_reading", wraps=store._reading) as reading:
            store.get_evidence(rec.id)
            store.get_evidence(rec.id)
        assert reading.call_count == 2

    def test_a_write_by_the_run_ends_what_it_read(self, store, rec):
        with store.read_memo():
            assert store.get_frame_rate(rec.id) == (False, None)
            store.set_frame_rate(rec.id, 25.0, identity=(rec.size, rec.mtime_ns))
            assert store.get_frame_rate(rec.id) == (True, 25.0)

    def test_a_write_to_what_it_read_by_another_thread_ends_it_too(self, store, rec):
        with store.read_memo():
            assert store.get_frame_rate(rec.id) == (False, None)
            writer = threading.Thread(
                target=store.set_frame_rate, args=(rec.id, 25.0), kwargs={"identity": (rec.size, rec.mtime_ns)}
            )
            writer.start()
            writer.join()
            assert store.get_frame_rate(rec.id) == (True, 25.0)

    def test_failure_lookups_are_made_once_inside_a_run(self, store, rec):
        identity = FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)
        with store.read_memo(), patch.object(store, "_reading", wraps=store._reading) as reading:
            for _ in range(3):
                assert store.get_detector_failure(rec.id, Source.SEASON_AUDIO) is None
                assert store.credits_text_timed_out_at(identity) is None
        assert reading.call_count == 2

    def test_a_detector_failure_another_thread_records_is_seen(self, store, rec):
        with store.read_memo():
            assert store.get_detector_failure(rec.id, Source.SEASON_AUDIO) is None
            writer = threading.Thread(target=store.set_detector_failure, args=(rec.id, Source.SEASON_AUDIO, "sig"))
            writer.start()
            writer.join()
            assert store.get_detector_failure(rec.id, Source.SEASON_AUDIO) == "sig"

    @pytest.mark.parametrize(
        "record, read",
        [
            ("record_member_probe_failure", "member_probe_failed_at"),
            ("record_member_fingerprint_failure", "member_fingerprint_failed_at"),
            ("record_end_picture_failure", "end_picture_failed_at"),
            ("record_credits_text_timeout", "credits_text_timed_out_at"),
        ],
    )
    def test_an_identity_failure_another_thread_records_is_seen(self, store, rec, record, read):
        identity = FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)
        failed_at = datetime(2026, 10, 1, tzinfo=UTC)
        with store.read_memo():
            assert getattr(store, read)(identity) is None
            writer = threading.Thread(
                target=getattr(store, record),
                args=(identity, failed_at),
                kwargs={"forget_before": failed_at - timedelta(days=1)},
            )
            writer.start()
            writer.join()
            assert getattr(store, read)(identity) == failed_at

    def test_a_list_handed_out_can_be_changed_without_changing_the_next_read(self, store, rec):
        with store.read_memo():
            store.get_evidence(rec.id).append("not evidence")
            assert store.get_evidence(rec.id) == []

    def test_another_threads_run_does_not_share_the_memo(self, store, rec):
        seen = []
        with store.read_memo():
            store.get_file(rec.canonical_path)
            thread = threading.Thread(target=lambda: seen.append(store._current_memo()))
            thread.start()
            thread.join()
        assert seen == [None]


class TestUpsertOfAFileAlreadyStored:
    def _upsert(self, store, path, **kwargs):
        args = {"duration_ms": 1000, "season_key": "/s", "is_movie": False} | kwargs
        return store.upsert_file(FileIdentity(path, 10, 20), **args)

    def test_a_recheck_of_an_unchanged_file_moves_only_when_it_was_last_looked_at(self, tmp_path):
        now = [datetime(2026, 10, 1, tzinfo=UTC)]
        store = MarkerStore(str(tmp_path / "clock.db"), clock=lambda: now[0])
        path = str(tmp_path / "a.mkv")
        first = self._upsert(store, path)
        generation = store.data_generation
        now[0] += timedelta(hours=1)
        assert self._upsert(store, path) == first
        assert self._upsert(store, path, duration_ms=None) == first  # an unknown length keeps the stored one
        updated = store._conn.execute("SELECT updated_at FROM files WHERE id=?", (first.id,)).fetchone()[0]
        assert updated == now[0].isoformat()
        assert store.data_generation == generation
        store.close()

    def test_a_file_marked_missing_is_marked_present_again_by_the_same_call(self, tmp_path):
        store = MarkerStore(str(tmp_path / "missing.db"))
        path = str(tmp_path / "a.mkv")
        first = self._upsert(store, path)
        with store._tx() as conn:
            conn.execute("UPDATE files SET missing_since=? WHERE id=?", ("2026-10-01T00:00:00", first.id))
        assert self._upsert(store, path) == first
        row = store._conn.execute("SELECT missing_since FROM files WHERE id=?", (first.id,)).fetchone()
        assert row[0] is None
        store.close()

    @pytest.mark.parametrize(
        "change",
        [{"duration_ms": 2000}, {"season_key": "/other"}, {"is_movie": True}],
        ids=["duration", "season", "kind"],
    )
    def test_a_change_is_written(self, store, tmp_path, change):
        path = str(tmp_path / "a.mkv")
        self._upsert(store, path)
        generation = store.data_generation
        self._upsert(store, path, **change)
        assert store.data_generation == generation + 1

    def test_a_file_marked_missing_is_marked_present_again(self, store, tmp_path):
        path = str(tmp_path / "a.mkv")
        rec = self._upsert(store, path)
        assert store.mark_missing(rec)
        again = self._upsert(store, path)
        assert again.missing_since is None
