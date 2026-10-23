"""Tests for library_health.failures: files a feature can never finish."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome
from media_preview_generator.library_health import failures
from media_preview_generator.library_health.failures import NO_DURATION_MESSAGE
from media_preview_generator.library_health.models import Feature
from media_preview_generator.library_health.store import HealthStore
from media_preview_generator.markers import pipeline
from media_preview_generator.processing.types import ProcessableItem


@pytest.fixture
def store(tmp_path, monkeypatch):
    opened = HealthStore(str(tmp_path / "health.db"))
    monkeypatch.setattr(failures, "default_store", lambda: opened)
    return opened


@pytest.fixture
def video(tmp_path) -> Path:
    path = tmp_path / "a.mkv"
    path.write_bytes(b"x" * 10)
    return path


def test_a_recorded_failure_counts_only_while_the_file_is_unchanged(store, video):
    failures.record_failure((Feature.PREVIEWS,), str(video), "no decoder")

    assert failures.unreadable_paths(store) == {Feature.PREVIEWS: frozenset({str(video)})}

    video.write_bytes(b"y" * 20)

    assert failures.unreadable_paths(store) == {}


def test_a_record_for_a_missing_file_is_ignored(store, video):
    failures.record_failure((Feature.PREVIEWS,), str(video), "no decoder")
    video.unlink()

    assert failures.unreadable_paths(store) == {}


def test_clearing_forgets_the_record(store, video):
    failures.record_failure((Feature.INTRO, Feature.CREDITS), str(video), "x")
    failures.clear_failure((Feature.INTRO,), str(video))

    assert set(failures.unreadable_paths(store)) == {Feature.CREDITS}


def test_recording_a_file_that_cannot_be_stat_ed_never_raises(store, tmp_path):
    failures.record_failure((Feature.PREVIEWS,), str(tmp_path / "gone.mkv"), "x")

    assert store.failures() == {}


def _run_item(path: Path, outcome: ItemOutcome | None, stage: str) -> None:
    """Run one intro & credits stage on ``path`` with the file's pipeline run returning ``outcome``."""
    item = ProcessableItem(canonical_path=str(path), server_id="s1", title="t")
    with patch.object(pipeline, "_run", return_value=outcome):
        if stage == "check":
            pipeline.check_item(item, ctx=MagicMock())
        else:
            pipeline.process_item(item, ctx=MagicMock())


STAGES = ["check", "worker"]


@pytest.mark.parametrize("stage", STAGES)
def test_a_file_whose_duration_cant_be_read_is_recorded_for_intro_and_credits(store, video, stage):
    _run_item(video, ItemOutcome("failed", NO_DURATION_MESSAGE), stage)

    assert set(failures.unreadable_paths(store)) == {Feature.INTRO, Feature.CREDITS}


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("key", ["markers_published", "markers_up_to_date", "markers_none"])
def test_a_file_intro_and_credits_later_finish_clears_the_record(store, video, key, stage):
    failures.record_failure((Feature.INTRO, Feature.CREDITS), str(video), "x")

    _run_item(video, ItemOutcome(key), stage)

    assert failures.unreadable_paths(store) == {}


@pytest.mark.parametrize(
    "key",
    ["skipped_file_not_found", "markers_no_owners", "markers_skipped", "markers_waiting", "skipped_source_gone"],
)
def test_an_outcome_that_never_read_the_duration_keeps_the_record(store, video, key):
    failures.record_failure((Feature.INTRO, Feature.CREDITS), str(video), "x")

    _run_item(video, ItemOutcome(key), "check")

    assert set(failures.unreadable_paths(store)) == {Feature.INTRO, Feature.CREDITS}


def test_a_file_handed_on_to_a_worker_keeps_the_record(store, video):
    failures.record_failure((Feature.INTRO, Feature.CREDITS), str(video), "x")

    _run_item(video, None, "check")

    assert set(failures.unreadable_paths(store)) == {Feature.INTRO, Feature.CREDITS}


def test_a_different_failure_is_not_recorded(store, video):
    _run_item(video, ItemOutcome("failed", "something else"), "worker")

    assert store.failures() == {}
    assert os.path.exists(video)


def test_a_store_reopened_on_the_database_still_clears_an_earlier_record(tmp_path, video):
    db = str(tmp_path / "health.db")
    HealthStore(db).record_failure(Feature.PREVIEWS, str(video), 10, 1, "no decoder")

    reopened = HealthStore(db)
    reopened.clear_failure(Feature.PREVIEWS, str(video))

    assert HealthStore(db).failures() == {}


def test_clearing_a_file_without_a_record_writes_nothing(store, video):
    failures.record_failure((Feature.PREVIEWS,), str(video), "no decoder")
    store._conn = MagicMock(wraps=store._conn)

    failures.clear_failure((Feature.INTRO, Feature.CREDITS), str(video))
    failures.clear_failure((Feature.PREVIEWS,), "/elsewhere/b.mkv")

    store._conn.execute.assert_not_called()
