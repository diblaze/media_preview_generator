"""A multi-path webhook batch retries the files that failed, not the first N.

``_run_webhook_paths_phase`` used to mark ``webhook_items[:failed_count]`` as
unresolved: with 3 paths and the 3rd one failing, it retried (and labelled
"Not found on any configured media server") the 1st one, which had succeeded,
and never retried the one that failed. The dispatch result now names the
failed files, from every place an item can fail.

A file that was dispatched was found and owned, so its failure (FFmpeg, a node that refused it) is reported as a
failure and retried, never as "not found on any server" with a path-mapping hint.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs import orchestrator
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker, reset_dispatcher
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.processing.multi_server import MultiServerResult, MultiServerStatus
from media_preview_generator.processing.types import ProcessableItem

MODULE = "media_preview_generator.jobs.orchestrator"


def _item(path: str) -> ProcessableItem:
    return ProcessableItem(canonical_path=path, server_id="", title=path.rsplit("/", 1)[-1])


def _config():
    config = MagicMock()
    config.cpu_threads = 1
    config.gpu_threads = 0
    config.worker_pool_timeout = 5
    return config


def _process_failing(failing: set[str]):
    """``process_canonical_path`` stand-in: every path generates except ``failing``."""

    def _side_effect(*args, **kwargs):
        path = kwargs.get("canonical_path", "")
        if kwargs.get("check_only"):
            return MultiServerResult(canonical_path=path, status=MultiServerStatus.NEEDS_GENERATION)
        time.sleep(0.01)
        status = MultiServerStatus.FAILED if path in failing else MultiServerStatus.PUBLISHED
        return MultiServerResult(canonical_path=path, status=status)

    return _side_effect


class TestWebhookPhaseRetriesTheFailedPaths:
    def _run_phase(self, raw_paths, canonical_by_raw, dispatch_result, unowned=()):
        config = SimpleNamespace(webhook_paths=raw_paths, webhook_item_id_hints=None)
        registry = MagicMock()
        registry.configs.return_value = []
        with (
            patch(
                "media_preview_generator.plex_client._expand_directory_to_media_files", side_effect=lambda p, m, **_: p
            ),
            patch(f"{MODULE}._log_webhook_owning_servers"),
            patch(
                f"{MODULE}._resolve_webhook_path_to_canonical",
                side_effect=lambda p, cfgs: (canonical_by_raw[p], [] if p in unowned else ["owner"]),
            ),
            # A hint for every path it's asked about, so a found file's row borrowing the not-found hint shows.
            patch(
                f"{MODULE}._build_path_mapping_mismatch_hints",
                side_effect=lambda paths, cfgs: {p: f"Add the webhook's path root for {p}" for p in paths},
            ),
        ):
            return orchestrator._run_webhook_paths_phase(
                config,
                registry,
                dispatch_items=lambda items, label: dispatch_result,
                progress_callback=None,
                cancel_check=None,
                job_id="job-1",
                totals={"processed": 0, "successful": 0, "failed": 0, "cancelled": False},
                aggregate_outcome={},
            )

    def test_the_failed_path_is_the_one_that_failed_when_a_later_item_fails(self):
        paths = ["/data/a.mkv", "/data/b.mkv", "/data/c.mkv"]
        result = self._run_phase(
            paths,
            {p: p for p in paths},
            {"completed": 2, "failed": 1, "cancelled": False, "outcome": {}, "failed_paths": ["/data/c.mkv"]},
        )

        assert result["failed_paths"] == ["/data/c.mkv"]
        assert result["unresolved_paths"] == []
        assert result["resolved_count"] == 3

    def test_failed_paths_use_the_raw_webhook_path_when_canonical_differs(self):
        canonical_by_raw = {"/tv/a.mkv": "/data/tv/a.mkv", "/tv/b.mkv": "/data/tv/b.mkv"}
        result = self._run_phase(
            list(canonical_by_raw),
            canonical_by_raw,
            {"completed": 1, "failed": 1, "cancelled": False, "outcome": {}, "failed_paths": ["/data/tv/b.mkv"]},
        )

        assert result["failed_paths"] == ["/tv/b.mkv"]

    def test_a_file_whose_processing_failed_is_a_failure_not_a_file_no_server_found(self):
        """Every server owns the file and FFmpeg failed on it (here, or on a node that refused it): no "not found"."""
        canonical_by_raw = {"/tv/a.mkv": "/data/tv/a.mkv"}
        result = self._run_phase(
            list(canonical_by_raw),
            canonical_by_raw,
            {"completed": 0, "failed": 1, "cancelled": False, "outcome": {}, "failed_paths": ["/data/tv/a.mkv"]},
        )

        assert result["unresolved_paths"] == []
        assert result["failed_paths"] == ["/tv/a.mkv"]
        assert result["path_hint_map"] == {} and result["path_hints"] == []
        assert result["resolved_count"] == 1

    def test_a_file_its_node_failed_is_not_unresolved_and_keeps_the_nodes_reason(self):
        canonical_by_raw = {"/tv/a.mkv": "/data/tv/a.mkv", "/tv/b.mkv": "/data/tv/b.mkv"}
        why = "node NAS unavailable (it dropped the file twice)"
        result = self._run_phase(
            list(canonical_by_raw),
            canonical_by_raw,
            {"completed": 0, "failed": 2, "cancelled": False, "outcome": {},
             "failed_paths": ["/data/tv/a.mkv", "/data/tv/b.mkv"], "node_failures": {"/data/tv/b.mkv": why}},
        )  # fmt: skip

        assert result["unresolved_paths"] == []
        assert result["failed_paths"] == ["/tv/a.mkv"]
        assert result["node_failed"] == {"/tv/b.mkv": why}
        assert result["path_hint_map"] == {}
        assert result["resolved_count"] == 2

    def test_a_path_no_server_owns_stays_unresolved_with_its_hint(self):
        canonical_by_raw = {"/elsewhere/x.mkv": "/elsewhere/x.mkv", "/tv/a.mkv": "/data/tv/a.mkv"}
        result = self._run_phase(
            list(canonical_by_raw),
            canonical_by_raw,
            {"completed": 0, "failed": 1, "cancelled": False, "outcome": {}, "failed_paths": ["/data/tv/a.mkv"]},
            unowned={"/elsewhere/x.mkv"},
        )

        assert result["unresolved_paths"] == ["/elsewhere/x.mkv"]
        assert result["failed_paths"] == ["/tv/a.mkv"]
        assert result["path_hint_map"] == {"/elsewhere/x.mkv": "Add the webhook's path root for /elsewhere/x.mkv"}
        assert result["resolved_count"] == 1

    def test_names_the_webhook_path_each_dispatched_file_came_from(self):
        """job_runner's retry scan reads file rows keyed by the path the file was dispatched under, and must
        resubmit the path the webhook sent for just the files still missing. The phase hands it that map."""
        canonical_by_raw = {"/tv/a.mkv": "/data/tv/a.mkv", "/tv/b.mkv": "/data/tv/b.mkv"}
        result = self._run_phase(
            list(canonical_by_raw),
            canonical_by_raw,
            {"completed": 2, "failed": 0, "cancelled": False, "outcome": {}, "failed_paths": []},
        )

        assert result["input_by_canonical"] == {"/data/tv/a.mkv": "/tv/a.mkv", "/data/tv/b.mkv": "/tv/b.mkv"}

    def test_nothing_unresolved_when_nothing_failed(self):
        paths = ["/data/a.mkv", "/data/b.mkv"]
        result = self._run_phase(
            paths,
            {p: p for p in paths},
            {"completed": 2, "failed": 0, "cancelled": False, "outcome": {}, "failed_paths": []},
        )

        assert result["unresolved_paths"] == []


class TestTrackerNamesFailedItems:
    """Every place a dispatcher item can fail records which file it was."""

    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_dispatcher()
        yield
        reset_dispatcher()

    @patch("media_preview_generator.processing.multi_server.process_canonical_path")
    def test_worker_failure_named_when_middle_item_fails(self, mock_process):
        mock_process.side_effect = _process_failing({"/data/b.mkv"})
        dispatcher = JobDispatcher(WorkerPool(gpu_workers=0, cpu_workers=2, selected_gpus=[]))
        try:
            tracker = dispatcher.submit_items(
                job_id="job-1",
                items=[_item("/data/a.mkv"), _item("/data/b.mkv"), _item("/data/c.mkv")],
                config=_config(),
                registry=MagicMock(),
            )
            assert tracker.wait(timeout=10)
        finally:
            dispatcher.shutdown()

        result = tracker.get_result()
        assert result["failed"] == 1
        assert result["failed_paths"] == ["/data/b.mkv"]

    def test_check_stage_failure_named(self):
        tracker = JobTracker(
            job_id="job-1", items=[_item("/data/a.mkv"), _item("/data/b.mkv")], config=_config(), registry=MagicMock()
        )

        with patch("media_preview_generator.processing.generator._notify_file_result"):
            tracker.record_check_result(
                _item("/data/a.mkv"), MultiServerResult(canonical_path="/data/a.mkv", status=MultiServerStatus.SKIPPED)
            )
            tracker.record_check_result(
                _item("/data/b.mkv"), MultiServerResult(canonical_path="/data/b.mkv", status=MultiServerStatus.FAILED)
            )

        assert tracker.get_result()["failed_paths"] == ["/data/b.mkv"]

    def test_unstarted_item_named(self):
        dispatcher = JobDispatcher(WorkerPool(gpu_workers=0, cpu_workers=1, selected_gpus=[]))
        try:
            tracker = JobTracker(job_id="job-1", items=[_item("/data/a.mkv")], config=_config(), registry=MagicMock())
            worker = dispatcher.worker_pool.workers[0]
            with patch("media_preview_generator.processing.generator._notify_file_result"):
                dispatcher._fail_unstarted_item(worker, tracker, _item("/data/a.mkv"), RuntimeError("no thread"))
        finally:
            dispatcher.shutdown()

        assert tracker.get_result()["failed_paths"] == ["/data/a.mkv"]

    def test_cancel_names_every_undispatched_item(self):
        tracker = JobTracker(
            job_id="job-1", items=[_item("/data/a.mkv"), _item("/data/b.mkv")], config=_config(), registry=MagicMock()
        )

        tracker.cancel()

        assert sorted(tracker.get_result()["failed_paths"]) == ["/data/a.mkv", "/data/b.mkv"]

    def test_no_failed_paths_when_all_succeed(self):
        tracker = JobTracker(job_id="job-1", items=[_item("/data/a.mkv")], config=_config(), registry=MagicMock())

        tracker.record_completion(True, "CPU 1", "a", canonical_path="/data/a.mkv")

        assert tracker.get_result()["failed_paths"] == []


class TestResumedWebhookJobReportsFailuresAsFailures:
    """A webhook job resumed after a restart sorts its failed files the same way as a fresh one."""

    def test_a_resumed_jobs_failed_file_is_a_failure_and_its_unowned_path_stays_unresolved(self, tmp_path):
        context = {
            "webhook_resolution": {
                "unresolved_paths": ["/elsewhere/x.mkv"],
                "skipped_paths": [],
                "total_paths": 3,
                "input_by_canonical": {"/data/tv/a.mkv": "/tv/a.mkv", "/data/tv/b.mkv": "/tv/b.mkv"},
                "path_hint_map": {"/elsewhere/x.mkv": "Add the webhook's path root"},
                "path_hints": ["Add the webhook's path root"],
            }
        }
        why = "node NAS unavailable (it dropped the file twice)"
        resumed = {
            "outcome": {"failed": 2},
            "failed_paths": ["/data/tv/a.mkv", "/data/tv/b.mkv"],
            "node_failures": {"/data/tv/b.mkv": why},
        }
        with (
            patch(f"{MODULE}._build_multi_server_registry", return_value=MagicMock()),
            patch(f"{MODULE}.checkpoint_items", return_value=[]),
            patch(f"{MODULE}._dispatch_processable_items", return_value=resumed),
        ):
            result = orchestrator.run_processing(
                SimpleNamespace(working_tmp_folder=str(tmp_path / "work")),
                [],
                continuation={"context": context, "state": {}},
                job_id="job-1",
            )

        resolution = result["webhook_resolution"]
        assert resolution["unresolved_paths"] == ["/elsewhere/x.mkv"]
        assert resolution["failed_paths"] == ["/tv/a.mkv"]
        assert resolution["node_failed"] == {"/tv/b.mkv": why}
        assert resolution["resolved_count"] == 2
