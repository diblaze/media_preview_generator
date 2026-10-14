import threading

import pytest

from media_preview_generator.markers.models import FileIdentity
from media_preview_generator.markers.store import MarkerStore


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _add_file(store, duration_ms=1000):
    ident = FileIdentity("/m/Show/Season 01/S01E01.mkv", 100, 1)
    return store.upsert_file(ident, duration_ms=duration_ms, season_key=None, is_movie=False)


def _run_in_thread(fn):
    """Run ``fn`` on its own thread; returns (finished, result)."""
    result = []
    t = threading.Thread(target=lambda: result.append(fn()), daemon=True)
    t.start()
    t.join(2)
    return not t.is_alive(), result[0] if result else None


def test_reads_do_not_wait_for_an_open_writer(store):
    rec = _add_file(store)
    writing = threading.Event()
    release = threading.Event()

    def writer():
        with store._tx() as conn:
            conn.execute("UPDATE files SET duration_ms=5555 WHERE id=?", (rec.id,))
            writing.set()
            release.wait(10)

    w = threading.Thread(target=writer, daemon=True)
    w.start()
    try:
        assert writing.wait(5)
        done, got = _run_in_thread(lambda: store.get_file(rec.canonical_path))
        assert done, "read waited for the writer"
        assert got.duration_ms == 1000  # the writer's change isn't committed yet
    finally:
        release.set()
        w.join(5)


def test_two_threads_read_at_once(store):
    rec = _add_file(store)
    inside = threading.Barrier(2, timeout=5)
    results = []

    def reader():
        with store._reading() as conn:
            inside.wait()  # both readers are inside their read block at the same moment
            results.append(conn.execute("SELECT id FROM files").fetchone()["id"])

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [rec.id, rec.id]


def test_read_inside_own_transaction_sees_uncommitted_write(store):
    rec = _add_file(store)
    with store._tx() as conn:
        conn.execute("UPDATE files SET duration_ms=7777 WHERE id=?", (rec.id,))
        assert store.get_file(rec.canonical_path).duration_ms == 7777


def test_other_thread_sees_a_committed_write(store):
    rec = _add_file(store)
    assert store.get_file(rec.canonical_path).duration_ms == 1000  # warms a pooled reader
    with store._tx() as conn:
        conn.execute("UPDATE files SET duration_ms=4242 WHERE id=?", (rec.id,))
    done, got = _run_in_thread(lambda: store.get_file(rec.canonical_path))
    assert done
    assert got.duration_ms == 4242
    assert store.get_file(rec.canonical_path).duration_ms == 4242  # the pooled reader isn't left on an old snapshot


def test_a_snapshot_reader_is_returned_to_the_pool_after_an_error(store):
    with pytest.raises(ValueError):
        with store._reading(snapshot=True):
            raise ValueError("boom")
    assert len(store._readers) == 1
    assert not store._readers[0].in_transaction


def test_a_plain_reader_that_failed_is_closed_not_pooled(store):
    # It may have left a cursor part-read, which would keep the next borrower on an older commit.
    with pytest.raises(ValueError):
        with store._reading() as conn:
            raise ValueError("boom")
    assert store._readers == []
    with pytest.raises(Exception, match="closed"):
        conn.execute("SELECT 1")


def test_plain_reads_run_no_transaction_statements(store):
    _add_file(store)
    store.get_file("/nope")
    (conn,) = store._readers
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    store.get_file("/nope")
    assert [s.split()[0].upper() for s in statements] == ["SELECT"]


def test_close_closes_pooled_readers(store):
    _add_file(store)
    store.get_file("/nope")
    readers = list(store._readers)
    assert readers
    store.close()
    assert store._readers == []
    for reader in readers:
        with pytest.raises(Exception, match="closed"):
            reader.execute("SELECT 1")


def test_a_reader_in_use_during_close_is_closed_not_pooled(store):
    with store._reading() as conn:
        store.close()
    assert store._readers == []
    with pytest.raises(Exception, match="closed"):
        conn.execute("SELECT 1")
