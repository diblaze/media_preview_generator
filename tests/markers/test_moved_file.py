"""A file moved to another folder keeps what Intro & Credits found for it (#414): its row follows it to the new path."""

from __future__ import annotations

import os
import shutil
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from media_preview_generator.markers import missing
from media_preview_generator.markers.audio import season
from media_preview_generator.markers.locks import FILE_RUN_LOCKS
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import ready_publisher
from tests.markers.pipeline_helpers import _ctx, _registry, _run

T = MarkerType
DUR = 1_417_088
OPENING = (Chapter(0, 92_000, "Intro"), Chapter(92_000, None, "Chapter 2"))
NAME = "Example Show (2026) - S01E12 - TBA [WEBDL-1080p][AAC 2.0][h264].mkv"
NOTHING = {"sources": [{"id": "theintrodb", "enabled": False}], "detect": {"intro": True, "credits": True}}


def _episode(tmp_path, show="Example Show (2026) {tvdb-1}", name=NAME):
    folder = tmp_path / "media" / "tv" / show / "Season 01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"x" * 1000)
    os.utime(path, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    return str(path)


def _move(old, tmp_path, folder="moved"):
    new = tmp_path / "media" / folder / "Example Show (2026) {tvdb-1}" / "Season 01" / os.path.basename(old)
    new.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old, new)  # a move keeps the size and the modified time
    return str(new)


def _process(store, path, stype=ServerType.PLEX, item_id=None, publisher=None):
    registry = _registry(path, stype)
    sid = f"{stype.value}-1"
    if item_id is not None:
        registry.get(sid).resolve_remote_path_to_item_id.return_value = item_id
    ctx = _ctx(store, registry, settings_raw=NOTHING)
    out, probe = _run(ctx, path, {sid: publisher or ready_publisher()}, probe=MediaProbe(DUR, OPENING), stage="process")
    return out, probe, ctx


class TestPipeline:
    def test_a_moved_file_keeps_its_row_and_isnt_read_again(self, tmp_path, store):
        old = _episode(tmp_path)
        first, _, _ = _process(store, old)
        assert first.outcome_key == FileOutcome.PUBLISHED.value
        before = store.get_file(old)
        new = _move(old, tmp_path)
        out, probe, _ = _process(store, new)
        rec = store.get_file(new)
        assert rec.id == before.id
        assert store.get_file(old) is None
        assert rec.season_key == os.path.dirname(new)
        assert probe.call_count == 0  # its chapters, length and answers are kept: nothing is read from the file
        assert store.get_markers(rec.id)[T.INTRO] == Marker(T.INTRO, 0, 92_000, ("chapters",))
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value

    @pytest.mark.parametrize("stype", list(ServerType))
    def test_a_moved_file_on_a_new_server_item_is_sent_there(self, tmp_path, store, stype):
        # Emby and Jellyfin give a moved file a new item; Plex does when it doesn't match the move.
        old = _episode(tmp_path)
        _process(store, old, stype, item_id="item-old")
        new = _move(old, tmp_path)
        publisher = ready_publisher()
        out, probe, _ = _process(store, new, stype, item_id="item-new", publisher=publisher)
        assert probe.call_count == 0
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert publisher.write.call_args.args == ("item-new", [Marker(T.INTRO, 0, 92_000, ("chapters",))])
        assert publisher.write.call_args.kwargs["own_previous"] == [Marker(T.INTRO, 0, 92_000, ("chapters",))]

    def test_a_row_a_sibling_adopted_first_is_read_as_this_files_own(self, tmp_path, store):
        # A whole show moved: another episode's season step gives this file its old row between this run's lookup and
        # its own adoption.
        old = _episode(tmp_path)
        _process(store, old)
        before = store.get_file(old)
        new = _move(old, tmp_path)
        real = store.same_identity

        def sibling_first(size, mtime_ns, *, other_than):
            store.move_file(before.id, old, new)
            return real(size, mtime_ns, other_than=other_than)

        with patch.object(store, "same_identity", side_effect=sibling_first):
            out, probe, _ = _process(store, new)
        assert probe.call_count == 0  # read as the stored file it is, not a new one
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value

    def test_a_copy_still_at_the_old_path_is_another_file(self, tmp_path, store):
        old = _episode(tmp_path)
        _process(store, old)
        new = tmp_path / "media" / "copy" / os.path.basename(old)
        new.parent.mkdir(parents=True)
        shutil.copy2(old, new)  # same size and modified time, and the original is still there
        _, probe, _ = _process(store, str(new))
        assert store.get_file(str(new)).id != store.get_file(old).id
        assert probe.call_count == 1

    def test_a_file_moved_out_of_a_folder_left_empty_keeps_its_row(self, tmp_path, store):
        # Moving the file alone leaves its old folder empty, which marking a file missing reads as "can't tell".
        old = _episode(tmp_path)
        _process(store, old)
        before = store.get_file(old)
        new = tmp_path / "media" / "tv" / "Example Show (2026) {tvdb-1}" / "Season 1" / os.path.basename(old)
        new.parent.mkdir(parents=True)
        os.rename(old, new)
        assert os.listdir(os.path.dirname(old)) == []
        _, probe, _ = _process(store, str(new))
        assert store.get_file(str(new)).id == before.id
        assert probe.call_count == 0


def _stored(store, path, size=1000, mtime_ns=5, season_key=None):
    return store.upsert_file(FileIdentity(path, size, mtime_ns), duration_ms=DUR, season_key=season_key, is_movie=False)


class TestAdoptMoved:
    def test_of_several_gone_rows_the_one_looked_at_last_is_adopted(self, tmp_path):
        # Moved twice before this was kept: a row at each earlier path, the later one holding the newer results.
        times = iter(datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=n) for n in range(100))
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: next(times))
        first, later = _stored(store, str(tmp_path / "a.mkv")), _stored(store, str(tmp_path / "b.mkv"))
        _stored(store, first.canonical_path)  # looked at again last, though stored first
        moved = missing.adopt_moved(store, str(tmp_path / "c.mkv"), 1000, 5)
        assert moved.id == first.id
        assert store.get_file(later.canonical_path).id == later.id
        store.close()

    def test_a_row_whose_file_is_still_there_is_passed_over_for_one_that_is_gone(self, tmp_path):
        times = iter(datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=n) for n in range(100))
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: next(times))
        gone, there = _stored(store, str(tmp_path / "gone.mkv")), _stored(store, str(tmp_path / "there.mkv"))
        (tmp_path / "there.mkv").write_bytes(b"x")  # looked at last, but a copy is still at its path
        moved = missing.adopt_moved(store, str(tmp_path / "new.mkv"), 1000, 5)
        assert moved.id == gone.id and moved.canonical_path == str(tmp_path / "new.mkv")
        assert store.get_file(there.canonical_path).id == there.id
        store.close()

    def test_every_file_still_there_adopts_nothing(self, tmp_path, store):
        _stored(store, str(tmp_path / "a.mkv"))
        (tmp_path / "a.mkv").write_bytes(b"x")
        assert missing.adopt_moved(store, str(tmp_path / "b.mkv"), 1000, 5) is None

    def test_another_size_or_time_is_never_adopted(self, tmp_path, store):
        _stored(store, str(tmp_path / "a.mkv"))
        assert missing.adopt_moved(store, str(tmp_path / "b.mkv"), 1000, 6) is None
        assert missing.adopt_moved(store, str(tmp_path / "b.mkv"), 1001, 5) is None

    def test_a_disk_that_doesnt_answer_in_time_adopts_nothing(self, tmp_path, store, monkeypatch):
        _stored(store, str(tmp_path / "a.mkv"))
        monkeypatch.setattr(missing, "CHECK_TIMEOUT_S", 0.05)
        release = threading.Event()
        monkeypatch.setattr(missing, "_on_disk", lambda _path: release.wait(5) and False)
        try:
            assert missing.adopt_moved(store, str(tmp_path / "b.mkv"), 1000, 5) is None
        finally:
            release.set()
        assert store.get_file(str(tmp_path / "a.mkv")) is not None

    def test_a_file_another_run_holds_at_its_old_path_isnt_taken(self, tmp_path, store):
        old = _stored(store, str(tmp_path / "a.mkv"))
        with FILE_RUN_LOCKS.try_hold(old.canonical_path, 1) as held:
            assert held
            assert missing.adopt_moved(store, str(tmp_path / "b.mkv"), 1000, 5) is None
        assert store.get_file(old.canonical_path).id == old.id

    def test_a_store_error_adopts_nothing(self, tmp_path, store):
        with patch.object(store, "same_identity", side_effect=OSError("disk I/O error")):
            assert missing.adopt_moved(store, str(tmp_path / "b.mkv"), 1000, 5) is None


class TestMoveFile:
    def test_the_row_takes_the_new_path_its_season_and_loses_its_missing_mark(self, tmp_path, store):
        old = _stored(store, "/tv/Show/Season 01/e1.mkv", season_key="/tv/Show/Season 01")
        assert store.mark_missing(old)
        moved = store.move_file(old.id, old.canonical_path, "/tv2/Show/Season 01/e1.mkv")
        assert (moved.id, moved.season_key, moved.missing_since) == (old.id, "/tv2/Show/Season 01", None)

    def test_a_movie_keeps_no_season(self, store):
        old = _stored(store, "/movies/A (2020)/A.mkv")
        assert store.move_file(old.id, old.canonical_path, "/movies2/A (2020)/A.mkv").season_key is None

    def test_a_row_already_at_the_new_path_stops_it(self, store):
        old, taken = _stored(store, "/a.mkv"), _stored(store, "/b.mkv", size=7)
        assert store.move_file(old.id, "/a.mkv", "/b.mkv") is None
        assert (store.get_file("/a.mkv").id, store.get_file("/b.mkv").id) == (old.id, taken.id)

    def test_a_row_no_longer_at_the_old_path_stops_it(self, store):
        old = _stored(store, "/a.mkv")
        assert store.move_file(old.id, "/elsewhere.mkv", "/b.mkv") is None
        assert store.get_file("/b.mkv") is None

    @pytest.mark.parametrize(
        ("record", "failed_at"),
        [
            ("record_member_probe_failure", "member_probe_failed_at"),
            ("record_member_fingerprint_failure", "member_fingerprint_failed_at"),
            ("record_end_picture_failure", "end_picture_failed_at"),
            ("record_credits_text_timeout", "credits_text_timed_out_at"),
        ],
    )
    def test_failures_kept_by_path_follow_it(self, store, record, failed_at):
        old = _stored(store, "/a.mkv")
        now = datetime.now(UTC)
        getattr(store, record)(FileIdentity("/a.mkv", 1000, 5), now, forget_before=now - timedelta(days=1))
        store.move_file(old.id, "/a.mkv", "/b.mkv")
        assert getattr(store, failed_at)(FileIdentity("/b.mkv", 1000, 5)) is not None
        assert getattr(store, failed_at)(FileIdentity("/a.mkv", 1000, 5)) is None


class TestSeasonMember:
    def test_a_moved_sibling_the_season_step_reads_first_keeps_its_row(self, tmp_path, store):
        # A whole show moved: the season step of one episode reads its siblings before their own runs do.
        sibling = _episode(tmp_path, name="Example Show (2026) - S01E13 - TBA.mkv")
        _process(store, sibling)
        before = store.get_file(sibling)
        new = _move(sibling, tmp_path)
        ctx = _ctx(store, _registry(new, ServerType.PLEX), settings_raw=NOTHING)
        with patch.object(season, "probe_media") as probe:
            rec = season._member_record(ctx, new, with_frame_rate=False)
        assert rec.id == before.id and rec.canonical_path == new
        assert probe.call_count == 0
