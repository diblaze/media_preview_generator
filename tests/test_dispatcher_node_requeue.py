"""Files a remote worker gives back: queued again for another try, or failed once (spec §6)."""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome, KindHandlers
from media_preview_generator.jobs import dispatcher as dispatcher_mod
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker, reset_dispatcher
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.nodes.executor import FileNotVisibleError, NodeUnavailableError, current_placement
from media_preview_generator.nodes.registry import STATE_OFFLINE, STATE_READY, NodeRegistry, NodeView, set_registry
from media_preview_generator.processing.types import ProcessableItem

KEYS = ("markers_published", "failed")


class _Registry(NodeRegistry):
    """Node views for the pool's suspect-node hold. ``fresh`` nodes answer every re-check at once (ready, just seen);
    the others keep their last view until :meth:`back` (or :meth:`down`)."""

    def __init__(self, *, fresh: bool = True) -> None:
        self.fresh = fresh
        self.states: dict[str, tuple[str, float]] = {}
        self._clock = itertools.count(1)

    def view(self, node_id: str) -> NodeView | None:
        if self.fresh:
            state, seen = STATE_READY, float(next(self._clock))
        else:
            state, seen = self.states.get(node_id, (STATE_READY, 0.0))
        return NodeView(node_id, node_id.upper(), "http://n", True, state, "", "1", ("worker",), (), 4, False, seen)

    def down(self, node_id: str) -> None:
        self.states[node_id] = (STATE_OFFLINE, self.states.get(node_id, (STATE_READY, 0.0))[1])

    def back(self, node_id: str) -> None:
        self.states[node_id] = (STATE_READY, float(next(self._clock)))


@pytest.fixture(autouse=True)
def _reset():
    reset_dispatcher()
    yield
    reset_dispatcher()
    set_registry(None)


@pytest.fixture(autouse=True)
def registry():
    fake = _Registry()
    set_registry(fake)
    return fake


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


def _pool(*nodes: tuple[str | None, str], gpu_on: frozenset[str] = frozenset()) -> WorkerPool:
    """A pool of CPU workers, one per ``(node_id, node_name)``; a None id is this app's own worker. Workers on a node in
    ``gpu_on`` are GPU workers (the pool picks those first)."""
    pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
    for index, (node_id, node_name) in enumerate(nodes):
        if node_id in gpu_on:
            worker = Worker(index, "GPU", "NVIDIA", "cuda:0", 0, "RTX", node_id=node_id, node_name=node_name)
        else:
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
            assert tracker.wait(timeout=15)
    finally:
        dispatcher.shutdown()
    return tracker, dispatcher_rows.call_args_list + worker_rows.call_args_list


def _node() -> str | None:
    placement = current_placement()
    return None if placement is None else placement.node_id


class TestNodeDrops:
    def test_a_file_whose_only_node_drops_once_runs_there_again_once_it_checks_ready_and_counts_once(self):
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

    @pytest.mark.parametrize("node_back", [False, True], ids=["node-still-down", "node-checked-ready"])
    def test_a_dropped_file_retries_on_this_apps_worker_not_the_node_gpu_that_dropped_it(self, registry, node_back):
        registry.fresh = node_back  # back: the hold clears at once, so only the file's own preference keeps it away
        calls = []
        lock = threading.Lock()

        def process(item, **_kwargs):
            with lock:
                calls.append(_node())
                if len(calls) == 1:
                    raise NodeUnavailableError("n1", "NAS", "NAS stopped answering")
            return ItemOutcome("markers_published", "ok")

        tracker, rows = _run(_pool(("n1", "NAS"), (None, ""), gpu_on=frozenset({"n1"})), process, "/m/a.mkv")
        assert calls == ["n1", None]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert [r.args[0] for r in rows] == ["/m/a.mkv"]

    @pytest.mark.parametrize("with_local", [False, True], ids=["node-only", "node-and-this-app"])
    def test_a_node_restart_during_a_queue_fails_no_file(self, registry, with_local):
        registry.fresh = False
        paths = [f"/m/{n}.mkv" for n in range(8)]
        state = {"down": True, "refused": 0}
        done: list[tuple[str, str | None]] = []
        lock = threading.Lock()

        def bring_back():
            time.sleep(0.3)
            with lock:
                state["down"] = False
            registry.back("n1")

        def process(item, **_kwargs):
            node = _node()
            with lock:
                if node == "n1" and state["down"]:
                    state["refused"] += 1
                    if state["refused"] == 1:
                        registry.down("n1")
                        threading.Thread(target=bring_back, daemon=True).start()
                    raise NodeUnavailableError("n1", "NAS", "NAS couldn't start the task", started=False)
                done.append((item.canonical_path, node))
            return ItemOutcome("markers_published", "ok")

        nodes = [("n1", "NAS"), ("n1", "NAS")] + ([(None, "")] if with_local else [])
        tracker, rows = _run(_pool(*nodes, gpu_on=frozenset({"n1"})), process, *paths)
        assert (tracker.successful, tracker.failed) == (8, 0)
        assert sorted(path for path, _ in done) == paths
        assert all(r.args[1].value != "failed" for r in rows)
        # The node is held back after its first refusal: only the files already handed to its two workers hit it.
        assert 1 <= state["refused"] <= 2
        assert tracker.active_processing == 0

    def test_a_second_genuine_drop_fails_the_file_once(self):
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

    def test_starts_the_node_never_takes_dont_count_as_drops_but_are_capped(self, monkeypatch):
        monkeypatch.setattr(dispatcher_mod, "MAX_NODE_START_FAILURES", 3)
        calls = []

        def process(item, **_kwargs):
            calls.append(_node())
            raise NodeUnavailableError("n1", "NAS", "NAS couldn't start the task", started=False)

        tracker, rows = _run(_pool(("n1", "NAS")), process, "/m/a.mkv")
        assert calls == ["n1", "n1", "n1"]
        assert (tracker.successful, tracker.failed) == (0, 1)
        (row,) = rows
        assert row.args[2] == "the node couldn't start it 3 times (NAS)"
        assert tracker.node_drops == {}
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

    def test_a_check_thread_appending_while_the_queue_is_scanned_never_breaks_the_claim(self):
        pool = _pool(("a", "A"))
        busy_b = Worker(9, "CPU", node_id="b", node_name="B")
        busy_b.is_busy = True  # file 1 could still run on B, so it stays queued
        pool.workers.append(busy_b)
        tracker = self._tracker("/m/1.mkv", "/m/2.mkv")
        tracker.excluded_nodes = {"/m/1.mkv": {"a": "A"}}
        find = pool._find_available_worker
        appended: list[str] = []

        def find_while_a_check_thread_appends(*args, **kwargs):
            # A checking thread appends without the dispatcher's lock; do it mid-scan, deterministically.
            appended.append(f"/m/new{len(appended)}.mkv")
            tracker.item_queue.extend(_items(appended[-1]))
            return find(*args, **kwargs)

        pool._find_available_worker = find_while_a_check_thread_appends
        stranded: list = []

        worker, item = JobDispatcher(pool)._claim_worker(tracker, stranded)
        assert (worker.node_id, item.canonical_path) == ("a", "/m/2.mkv")
        assert [i.canonical_path for i in tracker.item_queue] == ["/m/1.mkv", *appended]
        assert stranded == []

    def test_concurrent_appends_during_claims_never_raise(self):
        pool = _pool(("a", "A"))
        tracker = self._tracker(*(f"/m/{n}.mkv" for n in range(64)))
        tracker.excluded_nodes = {f"/m/{n}.mkv": {"a": "A"} for n in range(64)}
        pool.workers.append(Worker(9, "CPU", node_id="b", node_name="B"))
        pool.workers[1].is_busy = True  # every file waits for B, so each claim scans the whole head
        dispatcher = JobDispatcher(pool)
        stop = threading.Event()

        def check_thread():
            n = 0
            while not stop.is_set():
                tracker.item_queue.extend(_items(f"/m/late{n}.mkv"))
                n += 1

        appender = threading.Thread(target=check_thread, daemon=True)
        appender.start()
        try:
            for _ in range(300):
                assert dispatcher._claim_worker(tracker, []) is None
        finally:
            stop.set()
            appender.join(timeout=5)
