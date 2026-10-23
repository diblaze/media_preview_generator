"""Streams stored as -inf: measured again once over the whole stream, remembered when really silent, and a stream whose
format changes for too long is remembered as one that can't be measured."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from media_preview_generator.loudness import analyze, job, plex_db, silent, undecodable

from .test_job import _streams, ctx, media  # noqa: F401 - fixtures
from .test_plex_db import FIELDS, FILE, SILENT, db  # noqa: F401 - the fixture
from .test_undecodable import FF, _fixed_identity, _part_hash, _run_with  # noqa: F401 - fixtures

MEASURED = {**FIELDS, "ln:loudness": "-21.75"}


def test_a_recorded_stream_reads_back(tmp_path):
    store = str(tmp_path / "loudness.db")
    assert silent.record("h1", 1, "eac3", path=store) is True
    silent.record("h1", 1, "eac3", path=store)
    silent.record("h2", 3, "dca", path=store)
    assert silent.read_keys(store) == {("h1", 1, "eac3"), ("h2", 3, "dca")}


@pytest.mark.parametrize("part_hash", [None, ""])
def test_a_part_without_a_hash_is_not_recorded(tmp_path, part_hash):
    store = str(tmp_path / "loudness.db")
    assert silent.record(part_hash, 1, "eac3", path=store) is False
    assert silent.read_keys(store) == frozenset()


def test_an_unwritable_or_unreadable_store_neither_raises_nor_counts(tmp_path):
    assert silent.record("h1", 1, "eac3", path=str(tmp_path / "absent" / "loudness.db")) is False
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    assert silent.read_keys(str(garbage)) is None


def _stored(db, stream_11, stream_12=MEASURED):  # noqa: F811
    plex_db.write_stream(db, 11, stream_11, deadline=1e12)
    plex_db.write_stream(db, 12, stream_12, deadline=1e12)


def _needed(ctx, media):  # noqa: F811
    cfg = job.owners(media, ctx.registry)[0]
    return [s.index for s in job._lookup(ctx, cfg, media)[2]]


def test_a_stream_stored_as_silent_is_measured_again_until_it_is_recorded(ctx, db, media):  # noqa: F811
    _stored(db, SILENT)
    assert _needed(ctx, media) == [1]  # stream 2 holds a real measurement

    silent.record(_part_hash(db), 1, "aac")
    assert _needed(ctx, media) == [1]  # a job reads the store once; another job's record waits
    ctx._silent = None  # the next job
    assert _needed(ctx, media) == []


def test_only_a_local_plex_database_measures_a_silent_stream_again(ctx, db, media):  # noqa: F811
    _stored(db, SILENT)
    stream = job.read_streams(db, [FILE], deadline=1e12)[0][0]
    assert job._remeasure_silent(ctx, db, stream) is True
    assert job._remeasure_silent(ctx, MagicMock(spec=job.RemoteLoudnessDb), stream) is False


@pytest.mark.parametrize("measured, recorded", [(MEASURED, False), (SILENT, True)])
def test_a_silent_stream_measured_again_is_replaced_and_remembered_only_when_still_silent(
    ctx,  # noqa: F811
    db,  # noqa: F811
    media,  # noqa: F811
    measured,
    recorded,
):
    _stored(db, SILENT)
    db.verify_streams = MagicMock()

    outcome, _mark_item = _run_with(db, ctx, media, run_many=AssertionError("one stream"), run=[measured])

    assert outcome.outcome_key == job.WRITTEN, outcome.message
    stored = plex_db.decode_extra_data(_streams(db)[11])[0]
    assert stored["ln:loudness"] == measured["ln:loudness"]
    assert silent.read_keys() == ({(_part_hash(db), 1, "aac")} if recorded else frozenset())
    assert _needed(ctx, media) == []  # replaced by a measurement, or remembered as silent


def test_a_new_stream_measured_silent_is_remembered_so_it_isnt_measured_again(ctx, db, media):  # noqa: F811
    plex_db.write_stream(db, 12, MEASURED, deadline=1e12)
    db.verify_streams = MagicMock()

    outcome, _mark_item = _run_with(db, ctx, media, run_many=AssertionError("one stream"), run=[SILENT])

    assert outcome.outcome_key == job.WRITTEN, outcome.message
    assert silent.read_keys() == {(_part_hash(db), 1, "aac")}
    ctx._silent = None
    assert _needed(ctx, media) == []


def test_a_stream_whose_format_changes_for_too_long_is_skipped_recorded_and_the_item_completes(ctx, db, media):  # noqa: F811
    plex_db.write_stream(db, 12, MEASURED, deadline=1e12)
    db.verify_streams = MagicMock()
    refused = analyze.LoudnessTooManyDropped("measuring it would drop 3391 frames; not stored")

    outcome, mark_item = _run_with(db, ctx, media, run_many=AssertionError("one stream"), run=refused)

    part_hash = _part_hash(db)
    assert outcome.outcome_key == job.UP_TO_DATE, outcome.message
    assert "stream 1 (aac) skipped: its audio format changes mid-track" in outcome.message
    assert undecodable.skipped_keys(FF) == {(part_hash, 1, "aac")}
    assert mark_item.call_args.kwargs["skipped"] == {(part_hash, 1, "aac")}
    assert job.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked
    assert _needed(ctx, media) == []


def test_a_silent_stream_whose_format_changes_for_too_long_keeps_its_value_and_isnt_measured_again(ctx, db, media):  # noqa: F811
    _stored(db, SILENT)
    db.verify_streams = MagicMock()
    refused = analyze.LoudnessTooManyDropped("measuring it would drop 3391 frames; not stored")

    outcome, _mark_item = _run_with(db, ctx, media, run_many=AssertionError("one stream"), run=refused)

    assert outcome.outcome_key == job.UP_TO_DATE, outcome.message
    assert plex_db.decode_extra_data(_streams(db)[11])[0]["ln:loudness"] == "-inf"
    assert undecodable.skipped_keys(FF) == {(_part_hash(db), 1, "aac")}
    assert silent.read_keys() == frozenset()
    assert _needed(ctx, media) == []


def test_a_silent_stream_whose_measurement_fails_is_measured_again_next_time(ctx, db, media):  # noqa: F811
    _stored(db, SILENT)
    db.verify_streams = MagicMock()

    outcome, _mark_item = _run_with(
        db, ctx, media, run_many=AssertionError("one stream"), run=analyze.LoudnessError("ffmpeg exited 1")
    )

    assert outcome.outcome_key == job.FAILED, outcome.message
    assert plex_db.decode_extra_data(_streams(db)[11])[0]["ln:loudness"] == "-inf"
    assert silent.read_keys() == frozenset() and undecodable.skipped_keys(FF) == frozenset()
    ctx._silent = ctx._undecodable = None  # the next job
    assert _needed(ctx, media) == [1]
