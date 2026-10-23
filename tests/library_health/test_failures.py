"""Tests for library_health.failures: files a feature can never finish."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome
from media_preview_generator.jobs.worker import Worker
from media_preview_generator.library_health import failures
from media_preview_generator.library_health.failures import NO_DURATION_MESSAGE
from media_preview_generator.library_health.models import Feature
from media_preview_generator.library_health.store import HealthStore
from media_preview_generator.loudness.job import OUTCOME_KEYS as LOUDNESS_OUTCOME_KEYS
from media_preview_generator.markers.outcomes import OUTCOME_KEYS as MARKER_OUTCOME_KEYS
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


def test_the_pipeline_still_words_the_duration_failure_the_way_the_job_matches_it():
    pipeline = Path(__file__).parents[2] / "media_preview_generator" / "markers" / "pipeline.py"

    assert f'"{NO_DURATION_MESSAGE}"' in pipeline.read_text()


def _run_item(path: Path, outcome: ItemOutcome, keys: tuple[str, ...]) -> None:
    worker = Worker(0, "CPU")
    with patch("media_preview_generator.jobs.worker._notify_file_result"):
        worker.assign_task(
            ProcessableItem(canonical_path=str(path), server_id="s1", title="t"),
            MagicMock(cpu_threads=0),
            MagicMock(),
            job_id="j",
            process_fn=lambda item, **_kw: outcome,
            outcome_keys=keys,
        )
        worker.current_thread.join(timeout=5)


def test_an_intro_job_that_cannot_read_the_duration_records_intro_and_credits(store, video):
    _run_item(video, ItemOutcome("failed", NO_DURATION_MESSAGE), MARKER_OUTCOME_KEYS)

    assert set(failures.unreadable_paths(store)) == {Feature.INTRO, Feature.CREDITS}


@pytest.mark.parametrize("key", ["markers_published", "markers_up_to_date", "markers_none"])
def test_an_intro_job_that_later_finishes_the_file_clears_the_record(store, video, key):
    failures.record_failure((Feature.INTRO, Feature.CREDITS), str(video), "x")

    _run_item(video, ItemOutcome(key), MARKER_OUTCOME_KEYS)

    assert failures.unreadable_paths(store) == {}


@pytest.mark.parametrize(
    "key",
    ["skipped_file_not_found", "markers_no_owners", "markers_skipped", "markers_waiting", "skipped_source_gone"],
)
def test_an_outcome_that_never_read_the_duration_keeps_the_record(store, video, key):
    failures.record_failure((Feature.INTRO, Feature.CREDITS), str(video), "x")

    _run_item(video, ItemOutcome(key), MARKER_OUTCOME_KEYS)

    assert set(failures.unreadable_paths(store)) == {Feature.INTRO, Feature.CREDITS}


def test_a_loudness_job_never_touches_the_marker_records(store, video):
    failures.record_failure((Feature.INTRO, Feature.CREDITS), str(video), "x")

    _run_item(video, ItemOutcome(next(k for k in LOUDNESS_OUTCOME_KEYS if k != "failed")), LOUDNESS_OUTCOME_KEYS)

    assert set(failures.unreadable_paths(store)) == {Feature.INTRO, Feature.CREDITS}


def test_a_different_failure_is_not_recorded(store, video):
    _run_item(video, ItemOutcome("failed", "something else"), MARKER_OUTCOME_KEYS)

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
