"""Retry summaries never become executable jobs when processing resumes."""

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.loudness import job as loudness_runner
from media_preview_generator.markers import job_runner as marker_runner
from media_preview_generator.web.jobs import JobManager
from media_preview_generator.web.routes import job_runner

pytestmark = pytest.mark.real_job_async


@pytest.fixture
def manager(tmp_path, monkeypatch):
    jm = JobManager(str(tmp_path))
    monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: jm)
    for runner in (job_runner, marker_runner, loudness_runner):
        monkeypatch.setattr(runner, "get_job_manager", lambda: jm)
    monkeypatch.setattr(
        "media_preview_generator.web.settings_manager.get_settings_manager",
        lambda: SimpleNamespace(processing_paused=False),
    )
    monkeypatch.setattr("media_preview_generator.web.webhooks.ensure_pending_webhook", lambda _: False)
    monkeypatch.setattr(job_runner, "_queue_intro_credits_follow_up", lambda *args: None)
    yield jm
    for runner in (job_runner, marker_runner, loudness_runner):
        for job in jm.get_all_jobs():
            runner._inflight_jobs.discard(job.id)
    jm.close()


def make_chain(manager, kind="previews"):
    head = manager.create_job(kind=kind, config={"selected_library_ids": ["original-library"]})
    manager.upsert_retry_chain_job(
        canonical_path="",
        basename="Original scan",
        attempt=1,
        max_attempts=3,
        next_run_at="2099-01-01T00:00:00+00:00",
        wait_seconds=60,
        outcome="scheduled",
        originating_job_id=head.id,
    )
    child = manager.create_job(kind=kind, config={"is_retry": True, "parent_job_id": head.id})
    return head, child


def test_global_resume_starts_ordinary_and_unheld_child_but_never_summary(manager):
    head, held_child = make_chain(manager)
    assert manager.request_pause(held_child.id)
    assert not manager.request_pause(head.id)
    ordinary = manager.create_job()
    child = manager.create_job(config={"is_retry": True, "parent_job_id": head.id})
    before = copy.deepcopy(head.config)
    with patch.object(job_runner.threading, "Thread") as thread:
        job_runner.resume_running_and_drain_pending()
        started = []
        for call in thread.call_args_list:
            target = call.kwargs["target"]
            closure = dict(
                zip(target.__code__.co_freevars, (cell.cell_contents for cell in target.__closure__), strict=True)
            )
            started.append(closure["job_id"])
    assert set(started) == {ordinary.id, child.id}
    assert head.id not in job_runner._inflight_jobs
    assert held_child.id not in job_runner._inflight_jobs
    assert manager.is_pause_requested(held_child.id)
    assert head.config == before
    assert "parked_checkpoint" not in head.config


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
@pytest.mark.parametrize("outcome", ["scheduled", "queued_for_slot", "running", "completed", "exhausted", None])
def test_summary_cannot_start_directly_even_during_child_terminal_transition(manager, kind, outcome):
    head, child = make_chain(manager, kind)
    manager.complete_job(child.id)
    if outcome is None:
        head.config.pop("last_outcome", None)
    else:
        head.config["last_outcome"] = outcome
    # An earlier accidental head run may have parked. Blocking dispatch must not discard that evidence.
    head.config["parked_checkpoint"] = "existing-head-checkpoint.json"
    head.config["resource_wait"] = {"reason": "configuration"}
    before = copy.deepcopy(head.config)
    direct = {
        "previews": job_runner._start_job_async,
        "intro_credits": marker_runner.start_intro_credits_job_async,
        "loudness": loudness_runner.start_loudness_job_async,
    }[kind]
    with patch.object(job_runner.threading, "Thread") as thread:
        direct(head.id, {"should_not_merge": True})
        job_runner._start_job_async(head.id)
    thread.assert_not_called()
    assert head.config == before
    if kind == "intro_credits":
        assert marker_runner._run_intro_credits_pass(head.id) is None
    elif kind == "loudness":
        assert loudness_runner._run_loudness_pass(head.id) is None
    assert head.config == before


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
def test_runner_rechecks_summary_identity_before_work_if_changed_after_thread_creation(manager, kind):
    head, child = make_chain(manager, kind)
    direct = {
        "previews": job_runner._start_job_async,
        "intro_credits": marker_runner.start_intro_credits_job_async,
        "loudness": loudness_runner.start_loudness_job_async,
    }[kind]
    with patch.object(job_runner.threading, "Thread") as thread:
        direct(child.id)
        assert thread.call_count == 1  # A real retry child remains executable.
        target = thread.call_args.kwargs["target"]
    child.config["is_retry_chain"] = True
    before = copy.deepcopy(child.config)
    with patch.object(manager, "start_job", wraps=manager.start_job) as start:
        target()
    start.assert_not_called()
    assert child.config == before
    assert "parked_checkpoint" not in child.config
