"""Files a remote worker gives back: queued again for another try, or failed once (spec §6)."""

from __future__ import annotations

import threading
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome, KindHandlers
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker, reset_dispatcher
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.nodes.executor import FileNotVisibleError, NodeUnavailableError, current_placement
from media_preview_generator.processing.types import ProcessableItem

KEYS = ("markers_published", "failed")


@pytest.fixture(autouse=True)
def _reset():
    reset_dispatcher()
    yield
    reset_dispatcher()


def _config():
    c = MagicMock()
    c.cpu_threads = 1
    c.gpu_threads = 0
    c.scan_workers = 2
    c.regenerate_thumbnails = False
    c.server_id_filter = None
    return c


def _items(*paths):
    return [ProcessableItem(canonical_path=p, server_id="s1", title=p) for p in paths]


def _pool(*nodes: tuple[str | None, str]) -> WorkerPool:
    """A pool of CPU workers, one per ``(node_id, node_name)``; a None id is this app's own worker."""
    pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
    for index, (node_id, node_name) in enumerate(nodes):
        worker = Worker(index, "CPU", node_id=node_id, node_name=node_name)
        worker.display_name = f"CPU Worker {index + 1}"
        worker._done_event = pool._worker_done_event
        pool.workers.append(worker)
    return pool


def _run(pool: WorkerPool, process, *paths: str):
    """Run ``paths`` through the dispatcher; returns the tracker and every ``_notify_file_result`` call."""
    dispatcher = JobDispatcher(pool)
    handlers = KindHandlers(check_fn=lambda item, *, cancel_check: None, process_fn=process, outcome_keys=KEYS)
    try:
        with (
            patch("media_preview_generator.processing.generator._notify_file_result") as dispatcher_rows,
            patch("media_preview_generator.jobs.worker._notify_file_result") as worker_rows,
            patch("media_preview_generator.web.jobs.get_job_manager"),
        ):
            tracker = dispatcher.submit_items(
                "j1", _items(*paths), _config(), MagicMock(), kind="intro_credits", handlers=handlers
            )
            assert tracker.wait(timeout=10)
    finally:
        dispatcher.shutdown()
    return tracker, dispatcher_rows.call_args_list + worker_rows.call_args_list


def _node() -> str | None:
    placement = current_placement()
    return None if placement is None else placement.node_id


class TestNodeDrops:
    def test_a_file_whose_node_drops_once_runs_again_and_counts_once(self):
        calls = []
        lock = threading.Lock()

        def process(item, **_kwargs):
            with lock:
                calls.append(_node())
                if len(calls) == 1:
                    raise NodeUnavailableError("n1", "NAS", "NAS stopped answering")
            return ItemOutcome("markers_published", "ok")

        tracker, rows = _run(_pool(("n1", "NAS")), process, "/m/a.mkv")
        assert calls == ["n1", "n1"]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert tracker.outcome_counts == {"markers_published": 1, "failed": 0}
        assert [r.args[0] for r in rows] == ["/m/a.mkv"]
        assert tracker.active_processing == 0

    def test_a_second_drop_fails_the_file_once(self):
        calls = []

        def process(item, **_kwargs):
            calls.append(item.canonical_path)
            raise NodeUnavailableError("n1", "NAS", "NAS stopped answering")

        tracker, rows = _run(_pool(("n1", "NAS")), process, "/m/a.mkv")
        assert calls == ["/m/a.mkv", "/m/a.mkv"]
        assert (tracker.successful, tracker.failed) == (0, 1)
        assert tracker.outcome_counts == {"markers_published": 0, "failed": 1}
        (row,) = rows
        assert row.args[0] == "/m/a.mkv" and row.args[1].value == "failed"
        assert row.args[2] == "node unavailable twice (NAS)"
        assert tracker.active_processing == 0


class TestNotVisible:
    @staticmethod
    def _hidden_on(*hidden: str):
        seen: list[tuple[str, str | None]] = []
        lock = threading.Lock()

        def process(item, **_kwargs):
            node = _node()
            with lock:
                seen.append((item.canonical_path, node))
            if node in hidden:
                raise FileNotVisibleError(node, node.upper(), "missing", f"{item.canonical_path} isn't there")
            return ItemOutcome("markers_published", "ok")

        return process, seen

    def test_a_file_one_node_cant_see_runs_on_another(self):
        process, seen = self._hidden_on("a")
        tracker, rows = _run(_pool(("a", "A"), ("b", "B")), process, "/m/1.mkv", "/m/2.mkv", "/m/3.mkv")
        assert (tracker.successful, tracker.failed) == (3, 0)
        assert sorted(path for path, node in seen if node == "b") == ["/m/1.mkv", "/m/2.mkv", "/m/3.mkv"]
        # Each file goes to the node that can't see it at most once.
        a_paths = [path for path, node in seen if node == "a"]
        assert len(a_paths) == len(set(a_paths))
        assert len(rows) == 3
        assert tracker.active_processing == 0

    def test_this_apps_own_worker_is_never_left_out(self):
        process, seen = self._hidden_on("a")
        tracker, _ = _run(_pool(("a", "A"), (None, "")), process, "/m/1.mkv")
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert seen[-1] == ("/m/1.mkv", None)

    def test_a_file_no_node_can_see_fails_once_naming_them(self):
        process, seen = self._hidden_on("a", "b")
        tracker, rows = _run(_pool(("a", "A"), ("b", "B")), process, "/m/1.mkv")
        assert sorted(node for _, node in seen) == ["a", "b"]
        assert (tracker.successful, tracker.failed) == (0, 1)
        (row,) = rows
        assert row.args[2] in ("not visible on A, B", "not visible on B, A")
        assert tracker.active_processing == 0


class TestClaimWorker:
    @staticmethod
    def _tracker(*paths: str) -> JobTracker:
        tracker = JobTracker(job_id="j", items=[], config=_config(), registry=MagicMock())
        tracker.item_queue = deque(_items(*paths))
        return tracker

    def test_files_a_free_node_cant_see_are_passed_over_for_one_it_can(self):
        pool = _pool(("a", "A"))
        tracker = self._tracker("/m/1.mkv", "/m/2.mkv")
        tracker.excluded_nodes = {"/m/1.mkv": {"a": "A"}}
        pool.workers.append(Worker(9, "CPU", node_id="b", node_name="B"))
        pool.workers[1].is_busy = True  # B can run file 1, but it's busy
        stranded: list = []

        worker, item = JobDispatcher(pool)._claim_worker(tracker, stranded)
        assert (worker.node_id, item.canonical_path) == ("a", "/m/2.mkv")
        assert [i.canonical_path for i in tracker.item_queue] == ["/m/1.mkv"]
        assert stranded == []

    def test_a_file_no_configured_worker_can_run_any_more_is_taken_off_the_queue_to_fail(self):
        pool = _pool(("a", "A"))
        tracker = self._tracker("/m/1.mkv")
        tracker.excluded_nodes = {"/m/1.mkv": {"a": "A", "b": "B"}}
        stranded: list = []

        assert JobDispatcher(pool)._claim_worker(tracker, stranded) is None
        assert [(t, i.canonical_path, reason) for t, i, reason in stranded] == [
            (tracker, "/m/1.mkv", "not visible on A, B")
        ]
        assert not tracker.item_queue and tracker.active_processing == 1
        assert not pool.workers[0].is_busy
