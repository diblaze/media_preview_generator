"""Streams FFmpeg can't decode: the durable store, the item mark, the job's handling and the real ffmpeg failure."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.loudness import analyze, job, plex_db, undecodable

from .test_job import _add_audio_stream, _item, _streams, ctx, media  # noqa: F401 - fixtures
from .test_plex_db import FIELDS, FILE, db  # noqa: F401 - the fixture

_REAL_IDENTITY = undecodable.ffmpeg_identity
FF = "/usr/bin/ffmpeg ffmpeg version 7.1"


def test_a_recorded_stream_reads_back_and_a_second_record_replaces_it(tmp_path):
    store = str(tmp_path / "loudness.db")
    assert undecodable.record("h1", 1, "av3a", "no decoder", FF, path=store) is True
    undecodable.record("h1", 1, "av3a", "still none", FF, path=store)
    undecodable.record("h1", 2, "av3a", "no decoder", FF, path=store)
    undecodable.record("h2", 1, "av3a", "no decoder", FF, path=store)

    assert undecodable.skipped_keys(FF, store) == {("h1", 1, "av3a"), ("h1", 2, "av3a"), ("h2", 1, "av3a")}
    assert undecodable.skipped_count_by_part_hash(FF, store) == {"h1": 2, "h2": 1}
    with sqlite3.connect(store) as conn:
        reason = conn.execute("SELECT reason FROM undecodable_streams WHERE part_hash = 'h1' AND stream_index = 1")
        assert reason.fetchone() == ("still none",)
        assert conn.execute("SELECT COUNT(*) FROM undecodable_streams").fetchone()[0] == 3


def test_a_different_ffmpeg_does_not_see_the_skips(tmp_path):
    store = str(tmp_path / "loudness.db")
    undecodable.record("h1", 1, "av3a", "no decoder", FF, path=store)
    other = "/opt/ffmpeg ffmpeg version 8.0"
    assert undecodable.skipped_keys(other, store) == frozenset()
    assert undecodable.skipped_count_by_part_hash(other, store) == {}


@pytest.mark.parametrize("part_hash", [None, ""])
def test_a_part_without_a_hash_is_not_recorded(tmp_path, part_hash):
    store = str(tmp_path / "loudness.db")
    assert undecodable.record(part_hash, 1, "av3a", "no decoder", FF, path=store) is False
    assert undecodable.skipped_keys(FF, store) == frozenset()


def test_a_store_that_cant_be_written_returns_false_instead_of_raising(tmp_path):
    assert undecodable.record("h1", 1, "av3a", "no decoder", FF, path=str(tmp_path / "absent" / "loudness.db")) is False


def test_a_missing_or_unreadable_store_reads_as_empty(tmp_path):
    absent = str(tmp_path / "absent" / "loudness.db")
    assert undecodable.skipped_keys(FF, absent) == frozenset()
    assert undecodable.skipped_count_by_part_hash(FF, absent) == {}
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"not a database" * 100)
    assert undecodable.skipped_keys(FF, str(garbage)) == frozenset()


def test_the_default_store_is_in_the_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    undecodable.record("h1", 1, "av3a", "no decoder", FF)
    assert (tmp_path / "loudness.db").exists()
    assert undecodable.skipped_keys(FF) == {("h1", 1, "av3a")}


def test_ffmpeg_identity_is_the_path_and_version_line_or_just_the_path(tmp_path):
    script = tmp_path / "ffmpeg"
    script.write_text("#!/bin/sh\necho 'ffmpeg version 9.9 Copyright'\necho second\n")
    script.chmod(0o755)
    assert _REAL_IDENTITY(str(script)) == f"{script.resolve()} ffmpeg version 9.9 Copyright"
    assert _REAL_IDENTITY(str(tmp_path / "nope")) == str(tmp_path / "nope")


def test_an_item_whose_only_unmeasured_stream_is_undecodable_is_marked(db):  # noqa: F811
    with sqlite3.connect(db._path()) as conn:
        conn.execute("UPDATE media_parts SET hash = 'h1' WHERE id = 1")
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)

    assert plex_db.mark_item(db, 1, deadline=1e12) is False  # stream 12 (index 2) has no loudness
    assert plex_db.mark_item(db, 1, deadline=1e12, skipped=frozenset({("h1", 2, "other")})) is False
    assert plex_db.mark_item(db, 1, deadline=1e12, skipped=frozenset({("h1", 2, "aac")})) is True


def _three_streams(db):  # noqa: F811
    """Streams 1 (av3a), 2 (eac3) and 3 (aac), as in a web release that muxes Audio Vivid first."""
    with sqlite3.connect(db._path()) as conn:
        conn.execute("UPDATE media_streams SET codec = 'av3a' WHERE id = 11")
        conn.execute("UPDATE media_streams SET codec = 'eac3' WHERE id = 12")
    _add_audio_stream(db, 13, 3)


@pytest.fixture(autouse=True)
def _fixed_identity(monkeypatch):
    monkeypatch.setattr(undecodable, "ffmpeg_identity", lambda _binary: FF)


def _part_hash(db):  # noqa: F811
    return job.read_streams(db, [FILE], deadline=1e12)[0][0].part_hash


def _run_with(db, ctx, media, *, run_many, run, record=None):  # noqa: F811
    patches = [
        patch.object(job, "GuardedLoudnessDb", plex_db.LocalPlexDb),
        patch.object(job.analyze, "run_many", side_effect=run_many),
        patch.object(job.analyze, "run", side_effect=run),
        patch.object(job, "mark_item", wraps=job.mark_item),
    ]
    if record is not None:
        patches.append(patch.object(undecodable, "record", record))
    with patches[0], patches[1], patches[2], patches[3] as mark_item:
        if record is not None:
            with patches[4]:
                return job.process_item(_item(media), ctx=ctx), mark_item
        return job.process_item(_item(media), ctx=ctx), mark_item


def _only_stream_1_has_no_decoder(_ffmpeg, _path, index, **_kwargs):
    if index == 1:
        raise analyze.LoudnessNoDecoder("ffmpeg exited 234: no decoder found for: none")
    return FIELDS


@pytest.mark.parametrize(
    "batch_error",
    [analyze.LoudnessError("batch failed"), analyze.LoudnessNoDecoder("ffmpeg exited 234: no decoder found for: none")],
)
def test_an_undecodable_stream_is_skipped_recorded_and_the_item_still_completes(ctx, db, media, batch_error):  # noqa: F811
    _three_streams(db)
    db.verify_streams = MagicMock()

    outcome, mark_item = _run_with(db, ctx, media, run_many=batch_error, run=_only_stream_1_has_no_decoder)

    part_hash = _part_hash(db)
    assert outcome.outcome_key == job.WRITTEN
    assert "stream 1 (av3a) skipped: FFmpeg can't decode it" in outcome.message
    stored = _streams(db)
    assert "ln:loudness" not in stored[11] and "ln:loudness" in stored[12] and "ln:loudness" in stored[13]
    assert undecodable.skipped_keys(FF) == {(part_hash, 1, "av3a")}
    with sqlite3.connect(undecodable.db_path()) as conn:
        assert conn.execute("SELECT part_hash, stream_index, codec, ffmpeg FROM undecodable_streams").fetchall() == [
            (part_hash, 1, "av3a", FF)
        ]
    assert mark_item.call_args.kwargs["skipped"] == {(part_hash, 1, "av3a")}
    assert job.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked
    assert [s.index for s in db.verify_streams.call_args.args[0]] == [2, 3]


def test_a_stream_that_cant_be_remembered_fails_the_file_and_leaves_the_item_unmarked(ctx, db, media):  # noqa: F811
    _three_streams(db)

    outcome, mark_item = _run_with(
        db,
        ctx,
        media,
        run_many=analyze.LoudnessError("batch failed"),
        run=_only_stream_1_has_no_decoder,
        record=lambda *_args, **_kwargs: False,
    )

    assert outcome.outcome_key == job.FAILED
    assert "couldn't be remembered" in outcome.message
    mark_item.assert_not_called()
    assert not job.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked


def test_a_recorded_stream_is_not_looked_up_again(ctx, db, media):  # noqa: F811
    _three_streams(db)
    cfg = job.owners(media, ctx.registry)[0]
    _row, _streams_, needed = job._lookup(ctx, cfg, media)
    assert [s.index for s in needed] == [1, 2, 3]

    undecodable.record(needed[0].part_hash, 1, "av3a", "no decoder", FF)
    _row, _streams_, needed = job._lookup(ctx, cfg, media)
    assert [s.index for s in needed] == [2, 3]


@pytest.mark.parametrize(
    "recorded, verified",
    [((1,), [2, 3]), ((1, 2, 3), [])],
)
def test_a_marked_item_is_verified_on_its_decodable_streams_only(ctx, db, media, recorded, verified):  # noqa: F811
    _three_streams(db)
    part_hash = _part_hash(db)
    for stream_id, index, codec in ((11, 1, "av3a"), (12, 2, "eac3"), (13, 3, "aac")):
        if index in recorded:
            undecodable.record(part_hash, index, codec, "no decoder", FF)
        else:
            plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    skipped = undecodable.skipped_keys(FF)
    assert plex_db.mark_item(db, 1, deadline=1e12, skipped=skipped) is True
    db.verify_streams = MagicMock()
    cfg = job.owners(media, ctx.registry)[0]

    with patch.object(job, "GuardedLoudnessDb", plex_db.LocalPlexDb):
        row, _streams_, needed = job._lookup(ctx, cfg, media)

    assert row is None and needed == []
    assert [s.index for s in db.verify_streams.call_args.args[0]] == verified


@pytest.mark.integration
def test_real_ffmpeg_reports_a_missing_decoder_for_one_track_and_measures_the_other(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("no ffmpeg")
    media_file = tmp_path / "two.mp4"
    noise = "anoisesrc=color=pink:seed=7:d=3"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", noise, "-f", "lavfi", "-i", noise, "-map", "0", "-map", "1",
         "-c:a", "aac", str(media_file)],
        check=True,
    )  # fmt: skip
    data = bytearray(media_file.read_bytes())
    entry = data.find(b"mp4a")  # the first track's sample entry: renamed to AV3A and its codec config hidden
    data[entry : entry + 4] = b"av3a"
    config = data.find(b"esds", entry)
    data[config : config + 4] = b"free"
    av3a = tmp_path / "av3a.mp4"
    av3a.write_bytes(bytes(data))

    with pytest.raises(analyze.LoudnessNoDecoder):
        analyze.run(ffmpeg, str(av3a), 0, duration_ms=3000)
    assert "ln:loudness" in analyze.run(ffmpeg, str(av3a), 1, duration_ms=3000)
