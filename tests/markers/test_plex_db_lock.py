"""The reader/writer lock on Plex's database file, and the 60 s trust of a READY local check for reads."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.base import (
    Capability,
    CapabilityReport,
    PublishError,
    cancellable_waits,
)
from media_preview_generator.markers.publishers.plex_db import DatabaseBusyError, _holding_db_lock
from media_preview_generator.markers.settings import ServerMarkersSettings
from tests.markers.test_plex_db_publisher import _publisher

READY = CapabilityReport(Capability.READY, "", {})


def _in_thread(fn):
    """Run ``fn`` on a thread; returns (thread, errors)."""
    errors: list[BaseException] = []

    def run():
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def _contend(db: str, seconds: float, *, shared: bool = False) -> list[BaseException]:
    """Try to take ``db``'s lock from another thread (one thread can't contend with itself); returns its errors."""

    def take():
        with _holding_db_lock(db, _deadline(seconds), shared=shared):
            pass

    thread, errors = _in_thread(take)
    thread.join(5)
    return errors


def _deadline(seconds: float) -> float:
    return time.monotonic() + seconds


class TestReadersShare:
    def test_two_read_connections_are_open_at_once(self, tmp_path):
        db = str(tmp_path / "plex.db")
        both_in, release = threading.Barrier(3, timeout=5), threading.Event()

        def reader():
            with _holding_db_lock(db, _deadline(5), shared=True):
                both_in.wait()
                release.wait(5)

        first, first_errors = _in_thread(reader)
        second, second_errors = _in_thread(reader)
        try:
            both_in.wait()
            assert plex_db._db_lock(db).locked()
        finally:
            release.set()
            first.join(5)
            second.join(5)
        assert first_errors == second_errors == []
        assert not plex_db._db_lock(db).locked()

    @pytest.mark.parametrize("outer_shared, inner_shared", [(True, True), (True, False), (False, True), (False, False)])
    def test_nesting_on_one_database_raises_instead_of_deadlocking(self, tmp_path, outer_shared, inner_shared):
        db = str(tmp_path / "plex.db")
        with _holding_db_lock(db, _deadline(1), shared=outer_shared):
            with pytest.raises(RuntimeError, match="already holds"):
                with _holding_db_lock(db, _deadline(1), shared=inner_shared):
                    pass
        with _holding_db_lock(db, _deadline(1)):
            pass

    def test_an_exclusive_holder_excludes_readers_with_the_same_error_as_before(self, tmp_path):
        db = str(tmp_path / "plex.db")
        with _holding_db_lock(db, _deadline(5)):
            (error,) = _contend(db, 0.2, shared=True)
        assert isinstance(error, DatabaseBusyError)
        assert "Another Intro & Credits task is still using this Plex database" in str(error)
        assert error.state is Capability.UNREACHABLE

    def test_a_reader_excludes_an_exclusive_holder(self, tmp_path):
        db = str(tmp_path / "plex.db")
        with _holding_db_lock(db, _deadline(5), shared=True):
            (error,) = _contend(db, 0.2)
        assert isinstance(error, DatabaseBusyError)

    def test_a_waiting_writer_blocks_new_readers(self, tmp_path):
        db = str(tmp_path / "plex.db")
        order: list[str] = []
        reader_in, release_first = threading.Event(), threading.Event()

        def first_reader():
            with _holding_db_lock(db, _deadline(5), shared=True):
                reader_in.set()
                release_first.wait(5)

        def writer():
            with _holding_db_lock(db, _deadline(5)):
                order.append("writer")

        def late_reader():
            with _holding_db_lock(db, _deadline(5), shared=True):
                order.append("late reader")

        first, _ = _in_thread(first_reader)
        reader_in.wait(5)
        writing, writer_errors = _in_thread(writer)
        lock = plex_db._db_lock(db)
        while lock._writers_waiting == 0:
            time.sleep(0.01)
        late, late_errors = _in_thread(late_reader)
        time.sleep(0.2)
        assert order == []
        release_first.set()
        for thread in (first, writing, late):
            thread.join(5)
        assert order == ["writer", "late reader"]
        assert writer_errors == late_errors == []

    def test_a_writer_that_gave_up_lets_readers_in_again(self, tmp_path):
        db = str(tmp_path / "plex.db")
        with _holding_db_lock(db, _deadline(5), shared=True):
            (error,) = _contend(db, 0.1)
            assert isinstance(error, DatabaseBusyError)
            assert _contend(db, 1, shared=True) == []

    def test_a_probe_inside_this_threads_open_connection_still_raises(self, tmp_path):
        db = tmp_path / "plex.db"
        db.write_bytes(b"")
        key = plex_db._lock_key(str(db))
        plex_db._thread_state.__dict__.setdefault("open", set()).add(key)
        try:
            with pytest.raises(RuntimeError):
                plex_db.shm_lock_held_elsewhere(str(db))
        finally:
            plex_db._thread_state.open.discard(key)

    @pytest.mark.parametrize("shared", [False, True], ids=["writer", "reader"])
    def test_cancel_while_waiting_still_stops_the_wait(self, tmp_path, shared):
        db = str(tmp_path / "plex.db")
        cancelled, errors = threading.Event(), []

        def waiter():
            with cancellable_waits(cancelled.is_set):
                try:
                    with _holding_db_lock(db, _deadline(30), shared=shared):
                        pass
                except PublishError as exc:
                    errors.append(exc)

        holder = _holding_db_lock(db, _deadline(5), shared=not shared)
        with holder:
            thread, _ = _in_thread(waiter)
            time.sleep(0.2)
            cancelled.set()
            thread.join(plex_db.WAIT_SLICE_S + 2)
        assert [str(e) for e in errors] == [plex_db.WAIT_CANCELLED]

    def test_a_reader_behind_a_writer_waiting_on_plex_names_plex(self, tmp_path):
        db = str(tmp_path / "plex.db")
        with _holding_db_lock(db, _deadline(5)):
            plex_db._db_lock(db).start_waiting_for_plex()
            (error,) = _contend(db, 0.1, shared=True)
        assert isinstance(error, DatabaseBusyError)
        assert "held by another program" in str(error)


class TestLocalCheckTrust:
    def _pub(self, tmp_path, provider=None):
        pub = _publisher(tmp_path, tmp_path / "plex", settings_provider=provider)
        pub._db = MagicMock()
        pub._db.file_checks.return_value = READY
        pub._db.item_exists.return_value = True
        return pub

    def test_a_ready_check_is_reused_for_reads_within_60_seconds(self, tmp_path, monkeypatch):
        pub = self._pub(tmp_path)
        now = [1000.0]
        monkeypatch.setattr(plex_db.time, "monotonic", lambda: now[0])
        assert pub.item_missing("7") is False
        now[0] += 59
        assert pub.item_missing("7") is False
        assert pub._db.file_checks.call_count == 1

    def test_the_check_runs_again_after_60_seconds(self, tmp_path, monkeypatch):
        pub = self._pub(tmp_path)
        now = [1000.0]
        monkeypatch.setattr(plex_db.time, "monotonic", lambda: now[0])
        pub.item_missing("7")
        now[0] += plex_db.LOCAL_CHECK_TRUST_S + 1
        pub.item_missing("7")
        assert pub._db.file_checks.call_count == 2

    def test_writes_and_capability_always_check_again(self, tmp_path):
        pub = self._pub(tmp_path)
        pub.item_missing("7")
        pub._local_checks(deadline=_deadline(5))
        pub._local_checks(deadline=_deadline(5), trust_recent=False)
        assert pub._db.file_checks.call_count == 3

    def test_a_check_that_isnt_ready_is_not_kept(self, tmp_path):
        pub = self._pub(tmp_path)
        pub._db.file_checks.return_value = CapabilityReport(Capability.UNREACHABLE, "Plex stopped", {})
        assert pub.item_missing("7") is None
        assert pub.item_missing("7") is None
        assert pub._db.file_checks.call_count == 2
        pub._db.file_checks.return_value = READY
        assert pub.item_missing("7") is False

    def test_a_failed_recheck_drops_the_trusted_answer(self, tmp_path):
        pub = self._pub(tmp_path)
        pub.item_missing("7")
        pub._db.file_checks.return_value = CapabilityReport(Capability.UNREACHABLE, "Plex stopped", {})
        assert not pub._local_checks(deadline=_deadline(5)).ready
        assert pub.item_missing("7") is None

    def test_the_settings_are_still_read_live(self, tmp_path):
        current = [ServerMarkersSettings(True, None, "2026-09-13T00:00:00+00:00", "restore")]
        pub = self._pub(tmp_path, provider=lambda: current[0])
        assert pub.item_missing("7") is False
        current[0] = ServerMarkersSettings(False, None, "2026-09-13T00:00:00+00:00", "restore")
        assert pub.item_missing("7") is None
        assert pub._db.file_checks.call_count == 1
