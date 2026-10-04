"""Retry accounting survives capped history, source relocation and another process."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.loudness import chain, job
from media_preview_generator.markers import job_runner as shared
from media_preview_generator.web.jobs import JobManager


@pytest.mark.parametrize("running", [False, True])
def test_cancel_chain_clears_live_countdowns_on_parent_and_child(tmp_path, running):
    manager = JobManager(config_dir=str(tmp_path))
    head = manager.create_job(kind="loudness", config={"is_retry_chain": True})
    child = manager.create_job(kind="loudness", config={"is_retry": True, "parent_job_id": head.id})
    for item in [head, child]:
        if running:
            manager.start_job(item.id)
        manager.set_job_outcome(item.id, {job.WAITING: 1})
        manager.update_progress(
            item.id, retry_eta="2099-01-01T00:00:00+00:00", retry_wait_total=30, current_item="Retry"
        )
    manager.cancel_job(head.id)
    reloaded = JobManager(config_dir=str(tmp_path))
    for item in [head, child]:
        persisted = reloaded.get_job(item.id)
        assert persisted.status.value == "cancelled"
        assert persisted.progress.retry_eta is None and persisted.progress.retry_wait_total is None
        assert persisted.progress.current_item == ""
        assert persisted.progress.outcome == {job.WAITING: 1}


def test_reloaded_retry_replaces_missing_sender_once_without_losing_unlisted_successes(tmp_path):
    manager = JobManager(config_dir=str(tmp_path))
    head = manager.create_job(kind="loudness")
    manager.set_job_outcome(head.id, {job.WRITTEN: 10000, job.FILE_NOT_FOUND: 1})
    publisher = {"server_id": "plex", "server_name": "Plex", "server_type": "plex", "counts": {job.WRITTEN: 10000}}
    manager.set_publishers(head.id, [publisher])
    baseline = chain.snapshot(
        head, {"/sender/new.mkv": chain.previous_result("/old/missing.mkv", job.FILE_NOT_FOUND, [])}
    )
    retry = manager.create_job(
        kind="loudness",
        config={
            "retry_baseline": baseline,
            "retry_sender_paths": {"/new/mount/new.mkv": "/sender/new.mkv"},
        },
    )
    manager.record_file_result(
        retry.id,
        "/new/mount/new.mkv",
        job.UP_TO_DATE,
        servers=[{"server_id": "plex", "server_name": "Plex", "server_type": "plex", "status": job.UP_TO_DATE}],
    )
    # A row outside this attempt's bounded source snapshot cannot affect the chain's totals.
    manager.record_file_result(retry.id, "/unrelated/file.mkv", job.FAILED)
    reloaded = JobManager(config_dir=str(tmp_path))
    cfg = reloaded.get_job(retry.id).config
    for _ in range(2):
        outcome = job._recount_chain(reloaded, head.id, cfg, retry.id)
        assert outcome == {job.WRITTEN: 10000, job.UP_TO_DATE: 1}
        assert reloaded.get_job(head.id).publishers == [
            {**publisher, "counts": {job.WRITTEN: 10000, job.UP_TO_DATE: 1}}
        ]
    assert cfg["retry_baseline"] == baseline
    assert baseline["outcome"] == {job.WRITTEN: 10000, job.FILE_NOT_FOUND: 1}


def test_retry_countdown_after_global_pause_is_mirrored_to_visible_head(monkeypatch):
    now = datetime(2026, 10, 4, tzinfo=UTC)
    clock = [now]
    settings = SimpleNamespace(processing_paused=True)
    manager = MagicMock()
    monkeypatch.setattr(shared, "get_job_manager", lambda: manager)
    monkeypatch.setattr(shared, "get_settings_manager", lambda: settings)
    monkeypatch.setattr(shared, "_utcnow", lambda: clock[0])
    monkeypatch.setattr(shared, "_is_force_fire_now_set", lambda *_: False)

    def advance(_seconds):
        clock[0] += timedelta(seconds=1)
        settings.processing_paused = False

    monkeypatch.setattr(shared.time, "sleep", advance)
    due = now + timedelta(seconds=2)
    assert shared.wait_for_retry_time(
        "child", {"retry_not_before": due.isoformat()}, lambda: False, progress_job_id="head"
    )
    child = [call.kwargs for call in manager.update_progress.call_args_list if call.args == ("child",)]
    head = [call.kwargs for call in manager.update_progress.call_args_list if call.args == ("head",)]
    assert head == child
    assert [row["retry_eta"] for row in head] == [due.isoformat(), (due + timedelta(seconds=1)).isoformat(), None]
