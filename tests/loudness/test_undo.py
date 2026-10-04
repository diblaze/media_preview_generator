"""loudness.undo restores only tracks still holding what the job wrote."""

from __future__ import annotations

import builtins
import json
import shutil
import sqlite3

import pytest

from media_preview_generator.loudness import plex_db
from media_preview_generator.loudness.undo import main as undo
from media_preview_generator.loudness.undo import restored
from media_preview_generator.markers.publishers.base import PublishError
from media_preview_generator.markers.publishers.plex_db import decode_extra_data, encode_extra_data

from .test_plex_db import BEFORE, FIELDS, PLEX_ANALYSED, db  # noqa: F401 - the fixture


def _extra(path, stream_id):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT extra_data FROM media_streams WHERE id = ?", (stream_id,)).fetchone()[0]
    finally:
        conn.close()


def test_restores_what_it_wrote_and_leaves_what_changed_since(db, tmp_path):  # noqa: F811
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    plex_db.write_stream(db, 12, FIELDS, deadline=1e12)
    conn = sqlite3.connect(db._path())
    conn.execute("UPDATE media_streams SET extra_data = 'changed by Plex' WHERE id = 12")
    conn.commit()
    conn.close()
    log = str(tmp_path / "loudness-writes.jsonl")

    assert undo(["--db", db._path(), "--log", log, "--dry-run"]) == 0
    assert _extra(db._path(), 11) == PLEX_ANALYSED
    assert undo(["--db", db._path(), "--log", log]) == 0
    assert _extra(db._path(), 11) == BEFORE
    assert _extra(db._path(), 12) == "changed by Plex"


def test_the_items_mark_is_restored_with_its_tracks(db, tmp_path):  # noqa: F811
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    assert plex_db.mark_item(db, 1, deadline=1e12) is True
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 0
    conn = sqlite3.connect(db._path())
    try:
        assert conn.execute("SELECT extra_data FROM metadata_items WHERE id = 1").fetchone()[0] is None
    finally:
        conn.close()
    assert _extra(db._path(), 11) == BEFORE


def test_a_torn_last_line_is_skipped_and_the_rest_restored(db, tmp_path):  # noqa: F811
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    log = tmp_path / "loudness-writes.jsonl"
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"stream_id": 12, "bef')
    assert undo(["--db", db._path(), "--log", str(log)]) == 0
    assert _extra(db._path(), 11) == BEFORE


def test_a_mistyped_db_path_fails_and_creates_nothing(tmp_path):
    log = tmp_path / "loudness-writes.jsonl"
    log.write_text("")
    missing = tmp_path / "no-such.db"
    with pytest.raises(sqlite3.OperationalError):
        undo(["--db", str(missing), "--log", str(log)])
    assert not missing.exists()


def test_a_row_plex_changed_since_loses_only_the_fields_the_job_added(db, tmp_path):  # noqa: F811
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    assert plex_db.mark_item(db, 1, deadline=1e12) is True
    conn = sqlite3.connect(db._path())
    marked = conn.execute("SELECT extra_data FROM metadata_items WHERE id = 1").fetchone()[0]
    newer = encode_extra_data({**decode_extra_data(marked)[0], "pv:thumbBlurHash": "new poster"})
    conn.execute("UPDATE metadata_items SET extra_data = ? WHERE id = 1", (newer,))
    conn.commit()
    conn.close()
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 0
    conn = sqlite3.connect(db._path())
    try:
        item = conn.execute("SELECT extra_data FROM metadata_items WHERE id = 1").fetchone()[0]
    finally:
        conn.close()
    assert item == encode_extra_data({"pv:thumbBlurHash": "new poster"})  # the mark gone, Plex's change kept
    assert _extra(db._path(), 11) == BEFORE


def test_restored_leaves_a_row_whose_added_fields_changed():
    before = encode_extra_data({"ma:a": "1"})
    after = encode_extra_data({"ma:a": "1", "ln:loudness": "-20.00"})
    assert restored(before, after, after) == before
    assert restored(before, after, encode_extra_data({"ma:a": "2", "ln:loudness": "-20.00"})) == encode_extra_data(
        {"ma:a": "2"}
    )
    assert restored(before, after, encode_extra_data({"ma:a": "1", "ln:loudness": "-19.00"})) is False


def test_restored_leaves_a_row_it_cant_read():
    assert restored(None, encode_extra_data({"ln:loudness": "-20.00"}), "[1]") is False


def test_legacy_unscoped_records_are_refused(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    log = tmp_path / "loudness-writes.jsonl"
    log.write_text('{"stream_id": 1, "before": null, "after": "x"}\n')
    assert undo(["--db", str(empty), "--log", str(log)]) == 2


def test_undo_refuses_while_plex_has_the_database_open(db, tmp_path, monkeypatch):  # noqa: F811
    from media_preview_generator.loudness import undo as undo_module

    monkeypatch.setattr(undo_module, "shm_lock_held_elsewhere", lambda path: True)
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 2


def test_record_for_another_database_cannot_remove_native_analysis(db, tmp_path):  # noqa: F811
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    another = tmp_path / "another-plex.db"
    shutil.copyfile(db._path(), another)
    assert undo(["--db", str(another), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 2
    assert _extra(str(another), 11) == PLEX_ANALYSED
    assert _extra(db._path(), 11) == PLEX_ANALYSED


def test_shared_log_restores_only_requested_database(db, tmp_path):  # noqa: F811
    from media_preview_generator.markers.publishers.plex_db import LocalPlexDb

    another = tmp_path / "another-plex.db"
    shutil.copyfile(db._path(), another)
    other = LocalPlexDb(lambda: str(another))
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    plex_db.write_stream(other, 11, FIELDS, deadline=1e12)
    log = str(tmp_path / "loudness-writes.jsonl")
    assert undo(["--db", db._path(), "--log", log]) == 0
    assert _extra(db._path(), 11) == BEFORE
    assert _extra(str(another), 11) == PLEX_ANALYSED
    assert undo(["--db", str(another), "--log", log]) == 0
    assert _extra(str(another), 11) == BEFORE


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE media_streams SET created_at = 1234 WHERE id = 11",
        "UPDATE media_parts SET hash = 'replacement-source' WHERE id = 1",
        "UPDATE media_parts SET file = '/reused/row.mkv' WHERE id = 1",
    ],
)
def test_undo_preserves_reused_stream_target(db, tmp_path, mutation):  # noqa: F811
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    with sqlite3.connect(db._path()) as conn:
        conn.execute(mutation)
    conn.close()
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 0
    assert _extra(db._path(), 11) == PLEX_ANALYSED


def test_undo_preserves_a_reused_item_id(db, tmp_path):  # noqa: F811
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    plex_db.mark_item(db, 1, deadline=1e12)
    with sqlite3.connect(db._path()) as conn:
        before = conn.execute("SELECT extra_data FROM metadata_items WHERE id=1").fetchone()[0]
        conn.execute("UPDATE metadata_items SET guid='new-media-identity' WHERE id=1")
    conn.close()
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 0
    with sqlite3.connect(db._path()) as conn:
        assert conn.execute("SELECT extra_data FROM metadata_items WHERE id=1").fetchone()[0] == before
    conn.close()


def test_restoring_changed_fields_puts_previous_values_back():
    before = encode_extra_data({"ln:gainOffset": "old", "unrelated": "before"})
    after = encode_extra_data({"ln:gainOffset": "new", "unrelated": "before"})
    now = encode_extra_data({"ln:gainOffset": "new", "unrelated": "changed-by-plex"})
    assert restored(before, after, now) == encode_extra_data({"ln:gainOffset": "old", "unrelated": "changed-by-plex"})


def test_failed_commit_intent_cannot_undo_later_native_identical_measurements(db, tmp_path, monkeypatch):  # noqa: F811
    original_connect = db._connect

    class FailingCommit:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, *args):
            if statement == "COMMIT":
                raise sqlite3.OperationalError("simulated commit disk failure")
            return self.conn.execute(statement, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    with monkeypatch.context() as patch:
        patch.setattr(db, "_connect", lambda **kwargs: FailingCommit(original_connect(**kwargs)))
        with pytest.raises(PublishError):
            plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    assert _extra(db._path(), 11) == BEFORE
    log = tmp_path / "loudness-writes.jsonl"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [record["kind"] for record in records] == ["intent"]
    with sqlite3.connect(db._path()) as conn:
        conn.execute("UPDATE media_streams SET extra_data=? WHERE id=11", (PLEX_ANALYSED,))
    conn.close()
    assert undo(["--db", db._path(), "--log", str(log)]) == 0
    assert _extra(db._path(), 11) == PLEX_ANALYSED


def test_missing_commit_receipt_reports_committed_write_and_does_not_guess_on_undo(db, tmp_path, monkeypatch):  # noqa: F811
    real_open = builtins.open
    log = tmp_path / "loudness-writes.jsonl"
    appends = 0

    def failed_receipt(path, mode="r", *args, **kwargs):
        nonlocal appends
        if str(path) == str(log) and mode == "a":
            appends += 1
            if appends == 2:
                raise OSError("simulated receipt append failure")
        return real_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", failed_receipt)
        with pytest.raises(PublishError, match="committed") as caught:
            plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    assert "rolled back" not in str(caught.value)
    assert _extra(db._path(), 11) == PLEX_ANALYSED
    assert [json.loads(line)["kind"] for line in log.read_text().splitlines()] == ["intent"]
    assert undo(["--db", db._path(), "--log", str(log)]) == 0
    assert _extra(db._path(), 11) == PLEX_ANALYSED


def test_replaced_source_keeps_its_native_item_mark_and_streams(db, tmp_path):  # noqa: F811
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    plex_db.mark_item(db, 1, deadline=1e12)
    with sqlite3.connect(db._path()) as conn:
        marked = conn.execute("SELECT extra_data FROM metadata_items WHERE id=1").fetchone()[0]
        conn.execute("UPDATE media_parts SET hash='replacement-native-source' WHERE id=1")
    conn.close()
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 0
    assert _extra(db._path(), 11) == PLEX_ANALYSED
    with sqlite3.connect(db._path()) as conn:
        assert conn.execute("SELECT extra_data FROM metadata_items WHERE id=1").fetchone()[0] == marked
    conn.close()
