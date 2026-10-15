"""Files a remote worker gives back: queued again for another try, or failed once (spec §6)."""

from __future__ import annotations

import itertools
import json
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
from media_preview_generator.nodes.registry import (
    STATE_OFFLINE,
    STATE_READY,
    NodeCapacity,
    NodeRegistry,
    NodeView,
    set_registry,
)
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web.settings_manager import reset_settings_manager

KEYS = ("markers_published", "failed")


class _Registry(NodeRegistry):
    """Node views for the pool's suspect-node hold. ``fresh`` nodes answer every re-check at once (ready, just seen);
    the others keep their last view, and no check vouches for them since a drop, until :meth:`back` (or
    :meth:`down`)."""

    def __init__(self, *, fresh: bool = True) -> None:
        self.fresh = fresh
        self.states: dict[str, tuple[str, float]] = {}
        self.vouched: set[str] = set()
        self._clock = itertools.count(1)

    def checked_ready_since_unavailable(self, node_id: str) -> bool:
        return self.fresh or (node_id in self.vouched and self.states[node_id][0] == STATE_READY)

    def view(self, node_id: str) -> NodeView | None:
        if self.fresh:
            state, seen = STATE_READY, float(next(self._clock))
        else:
            state, seen = self.states.get(node_id, (STATE_READY, 0.0))
        return NodeView(node_id, node_id.upper(), "http://n", True, state, "", "1", ("worker",), (), 4, False, seen)

    def down(self, node_id: str) -> None:
        self.states[node_id] = (STATE_OFFLINE, self.states.get(node_id, (STATE_READY, 0.0))[1])
        self.vouched.discard(node_id)

    def back(self, node_id: str) -> None:
        self.states[node_id] = (STATE_READY, float(next(self._clock)))
        self.vouched.add(node_id)


@pytest.fixture(autouse=True)
def _reset():
    # A settings manager another test left behind would hand the dispatch loop its saved groups, replacing the pool's.
    reset_settings_manager()
    reset_dispatcher()
    yield
    reset_settings_manager()
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
        assert row.args[2] == "node NAS unavailable (it dropped the file twice)"
        # A webhook job reads these to word the file as the node's failure, never as one no server found.
        assert tracker.get_result()["failed_paths"] == ["/m/a.mkv"]
        assert tracker.get_result()["node_failures"] == {"/m/a.mkv": "node NAS unavailable (it dropped the file twice)"}
        assert tracker.active_processing == 0

    @pytest.mark.parametrize(("other", "other_name"), [("b", "B"), (None, "")], ids=["other-node", "this-app"])
    def test_a_dropped_file_waits_for_the_busy_worker_elsewhere_while_the_node_that_dropped_it_takes_the_next(
        self, other, other_name
    ):
        """A flapping node (it checks ready again right after each drop) would drop the file again: a second drop
        fails it, though the healthy worker that's busy now would run it a moment later."""
        calls: list[tuple[str, str | None]] = []
        lock = threading.Lock()

        def process(item, **_kwargs):
            node = _node()
            with lock:
                calls.append((item.canonical_path, node))
            if node == "a" and item.canonical_path == "/m/x.mkv":
                raise NodeUnavailableError("a", "A", "A stopped answering")
            if item.canonical_path == "/m/long.mkv":
                time.sleep(1.0)
            return ItemOutcome("markers_published", "ok")

        tracker, rows = _run(_pool((other, other_name), ("a", "A")), process, "/m/long.mkv", "/m/x.mkv", "/m/y.mkv")

        assert [node for path, node in calls if path == "/m/x.mkv"] == ["a", other]
        assert [node for path, node in calls if path == "/m/y.mkv"] == ["a"]
        assert [r.args[1].value for r in rows if r.args[1].value == "failed"] == []
        assert (tracker.successful, tracker.failed) == (3, 0)
        assert tracker.active_processing == 0

    def test_a_dropped_file_no_other_worker_can_run_goes_back_to_the_node_that_dropped_it(self):
        calls: list[str | None] = []
        lock = threading.Lock()

        def process(item, **_kwargs):
            node = _node()
            with lock:
                calls.append(node)
                drops_on_a = calls.count("a")
            if node == "b":
                raise FileNotVisibleError("b", "B", "missing", f"{item.canonical_path} isn't there")
            if drops_on_a == 1:
                raise NodeUnavailableError("a", "A", "A stopped answering")
            return ItemOutcome("markers_published", "ok")

        tracker, rows = _run(_pool(("b", "B"), ("a", "A")), process, "/m/x.mkv")

        assert calls == ["b", "a", "a"]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert [r.args[0] for r in rows] == ["/m/x.mkv"]

    @pytest.mark.parametrize("why", ["outside-its-hours", "gpu-not-listed"])
    def test_a_dropped_file_doesnt_wait_for_a_member_that_runs_nothing_now(self, monkeypatch, why):
        """Node B's member is outside its hours (back later) or names a GPU B doesn't list (back only once someone
        acts): waiting for it would hold the file and its job open for hours, while A is ready again."""
        from media_preview_generator import worker_groups

        class Registry(_Registry):
            revision = 0  # shadows the real registry's property

            def capacity(self, node_id: str) -> NodeCapacity:
                return NodeCapacity(True, True, "")

        set_registry(Registry())
        if why == "outside-its-hours":
            monkeypatch.setattr(
                worker_groups, "group_is_available", lambda policy, now=None: policy.get("group_id") != "b"
            )
            b_member = {"resource": "cpu", "device": None, "node": "b"}
        else:
            b_member = {"resource": "gpu", "device": "cuda:0", "node": "b"}

        def group(gid: str, member: dict) -> dict:
            return {"id": gid, "name": gid, "enabled": True, "availability": {"mode": "always", "windows": []},
                    "members": [{"id": "m", "count": 1, "job_types": ["intro_credits"], **member}]}  # fmt: skip

        pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
        pool.reconcile_groups([group("a", {"resource": "cpu", "device": None, "node": "a"}), group("b", b_member)], [])
        calls: list[str | None] = []

        def process(item, **_kwargs):
            calls.append(_node())
            if len(calls) == 1:
                raise NodeUnavailableError("a", "A", "A stopped answering")
            return ItemOutcome("markers_published", "ok")

        tracker, _ = _run(pool, process, "/m/x.mkv")

        assert calls == ["a", "a"]
        assert (tracker.successful, tracker.failed) == (1, 0)

    def test_one_drop_on_each_of_two_nodes_fails_naming_both(self):
        seen: list[str] = []
        lock = threading.Lock()

        def process(item, **_kwargs):
            node = _node()
            with lock:
                seen.append(node)
            raise NodeUnavailableError(node, node.upper(), f"{node.upper()} stopped answering")

        tracker, rows = _run(_pool(("a", "A"), ("b", "B")), process, "/m/1.mkv")

        assert sorted(seen) == ["a", "b"]  # the retry went to the other node
        (row,) = rows
        first, second = (name.upper() for name in seen)
        assert row.args[2] == f"nodes {first} and {second} unavailable (each dropped the file once)"
        assert tracker.node_failures == {"/m/1.mkv": row.args[2]}

    def test_a_file_the_worker_itself_fails_is_not_a_node_failure(self):
        def process(item, **_kwargs):
            raise RuntimeError("the detector broke")

        tracker, _ = _run(_pool(("n1", "NAS")), process, "/m/a.mkv")
        assert (tracker.successful, tracker.failed) == (0, 1)
        assert tracker.get_result()["failed_paths"] == ["/m/a.mkv"]
        assert tracker.get_result()["node_failures"] == {}

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
        assert row.args[2] == "node NAS unavailable (it couldn't start the file 3 times)"
        assert tracker.node_failures == {"/m/a.mkv": row.args[2]}
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
        assert tracker.node_failures == {"/m/1.mkv": row.args[2]}
        assert tracker.active_processing == 0

    def test_a_file_only_a_member_without_its_gpu_could_run_fails_naming_why(self):
        """Node B is ready but no longer lists the GPU its member names: nothing brings that member back without
        someone acting, so a file only it could run fails instead of keeping the job open forever."""

        class Registry(_Registry):
            revision = 0  # shadows the real registry's property

            def capacity(self, node_id: str) -> NodeCapacity:
                return NodeCapacity(True, True, "")

        set_registry(Registry())

        def group(gid: str, member: dict) -> dict:
            return {"id": gid, "name": gid, "enabled": True, "availability": {"mode": "always", "windows": []},
                    "members": [{"id": "m", "count": 1, "job_types": ["intro_credits"], **member}]}  # fmt: skip

        pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
        pool.reconcile_groups(
            [
                group("cpu-a", {"resource": "cpu", "device": None, "node": "a"}),
                group("gpu-b", {"resource": "gpu", "device": "cuda:0", "node": "b"}),
            ],
            [],
        )
        assert [w.node_id for w in pool.workers] == ["a"]
        process, _ = self._hidden_on("a")

        tracker, rows = _run(pool, process, "/m/1.mkv")

        assert (tracker.successful, tracker.failed) == (0, 1)
        (row,) = rows
        assert row.args[2] == "not visible on A; no other worker can take it: B (GPU not found on this node)"


class TestClaimWorker:
    @staticmethod
    def _tracker(*paths: str) -> JobTracker:
        tracker = JobTracker(job_id="j", items=[], config=_config(), registry=MagicMock())
        tracker.item_queue = deque(_items(*paths))
        return tracker

    def test_a_file_a_free_node_dropped_waits_for_the_busy_one_and_the_free_node_takes_the_next(self):
        pool = _pool(("a", "A"), ("b", "B"))
        pool.workers[1].is_busy = True
        tracker = self._tracker("/m/1.mkv", "/m/2.mkv")
        tracker.dropped_on = {"/m/1.mkv": "a"}

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])
        assert (worker.node_id, item.canonical_path) == ("a", "/m/2.mkv")
        assert [i.canonical_path for i in tracker.item_queue] == ["/m/1.mkv"]

    def test_a_file_a_node_dropped_goes_back_to_it_when_no_other_worker_runs_its_kind(self):
        pool = _pool(("a", "A"))
        tracker = self._tracker("/m/1.mkv")
        tracker.dropped_on = {"/m/1.mkv": "a"}

        worker, item = JobDispatcher(pool)._claim_worker(tracker, [])
        assert (worker.node_id, item.canonical_path) == ("a", "/m/1.mkv")
        assert not tracker.item_queue

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

    @staticmethod
    def _cancel_after_the_claim(pool: WorkerPool, tracker: JobTracker) -> None:
        """A web thread cancels the job right after the pool claims a worker, before the file leaves the queue."""
        find = pool._find_available_worker

        def find_then_cancel(*args, **kwargs):
            worker = find(*args, **kwargs)
            if worker is not None:
                tracker.cancel()
            return worker

        pool._find_available_worker = find_then_cancel

    @pytest.mark.parametrize("excluded", [False, True], ids=["plain-queue", "files-some-nodes-cant-see"])
    def test_a_cancel_between_the_claim_and_taking_the_file_frees_the_worker(self, excluded):
        pool = _pool(("a", "A"))
        tracker = self._tracker("/m/1.mkv", "/m/2.mkv")
        if excluded:
            tracker.excluded_nodes = {"/m/1.mkv": {"b": "B"}}
        self._cancel_after_the_claim(pool, tracker)

        assert JobDispatcher(pool)._claim_worker(tracker, []) is None
        assert not pool.workers[0].is_busy and pool.workers[0].is_available()
        assert not tracker.item_queue and tracker.failed == 2

    def test_a_cancel_and_a_new_file_between_the_claim_and_taking_it_takes_neither(self):
        pool = _pool(("a", "A"))
        tracker = self._tracker("/m/1.mkv")
        tracker.excluded_nodes = {"/m/1.mkv": {"b": "B"}}
        find = pool._find_available_worker

        def find_then_cancel_and_append(*args, **kwargs):
            worker = find(*args, **kwargs)
            tracker.cancel()
            tracker.item_queue.extend(_items("/m/late.mkv"))
            return worker

        pool._find_available_worker = find_then_cancel_and_append

        assert JobDispatcher(pool)._claim_worker(tracker, []) is None
        assert [i.canonical_path for i in tracker.item_queue] == ["/m/late.mkv"]
        assert not pool.workers[0].is_busy

    def test_a_copy_interrupted_by_an_append_is_made_again(self):
        class AppendedDuringFirstCopy(deque):
            copies = 0

            def __iter__(self):
                AppendedDuringFirstCopy.copies += 1
                if AppendedDuringFirstCopy.copies == 1:
                    raise RuntimeError("deque mutated during iteration")
                return super().__iter__()

        queue = AppendedDuringFirstCopy(["/m/1.mkv", "/m/2.mkv"])

        assert dispatcher_mod._queue_head(queue) == ["/m/1.mkv", "/m/2.mkv"]
        assert AppendedDuringFirstCopy.copies == 2

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


class TestParkAndResume:
    """A parked job resumes with the per-file node counts it had: a park must not give a file fresh tries."""

    NODE_STATE = {
        "node_drops": {"/m/a.mkv": ["N1"]},
        "node_start_failures": {"/m/b.mkv": 4},
        "dropped_on": {"/m/a.mkv": "n1"},
        "excluded_nodes": {"/m/b.mkv": {"n2": "NAS"}},
    }

    def _parked_snapshot(self) -> dict:
        dispatcher = JobDispatcher(_pool(("n1", "N1")))
        tracker = JobTracker(job_id="j", items=_items("/m/a.mkv", "/m/b.mkv"), config=_config(), registry=MagicMock())
        for field, value in self.NODE_STATE.items():
            setattr(tracker, field, {path: (dict(v) if isinstance(v, dict) else v) for path, v in value.items()})
        tracker.park_requested = True
        dispatcher._trackers["j"] = tracker
        try:
            return dispatcher.park_snapshot("j")
        finally:
            dispatcher.shutdown()

    def test_the_node_counts_survive_a_checkpoint_round_trip(self, tmp_path):
        from media_preview_generator.jobs.checkpoints import read_checkpoint, write_checkpoint

        snapshot = self._parked_snapshot()
        reference = write_checkpoint(tmp_path, "j", snapshot)
        state = read_checkpoint(tmp_path, "j", reference)["state"]

        resumed = JobTracker(job_id="j", items=[], config=_config(), registry=MagicMock(), carried_state=state)

        for field, value in self.NODE_STATE.items():
            assert getattr(resumed, field) == value, field

    def test_a_snapshot_from_before_the_node_counts_resumes_with_none(self, tmp_path):
        from media_preview_generator.jobs.checkpoints import read_checkpoint, write_checkpoint

        snapshot = self._parked_snapshot()
        for field in self.NODE_STATE:
            del snapshot["state"][field]
        state = read_checkpoint(tmp_path, "j", write_checkpoint(tmp_path, "j", snapshot))["state"]

        resumed = JobTracker(job_id="j", items=[], config=_config(), registry=MagicMock(), carried_state=state)

        assert (resumed.node_drops, resumed.node_start_failures, resumed.dropped_on, resumed.excluded_nodes) == (
            {},
            {},
            {},
            {},
        )

    def test_a_snapshot_that_counted_drops_without_names_still_resumes(self, tmp_path):
        from media_preview_generator.jobs.checkpoints import read_checkpoint, write_checkpoint

        snapshot = self._parked_snapshot()
        reference = write_checkpoint(tmp_path, "j", snapshot)
        path = tmp_path / "job_checkpoints" / reference
        payload = json.loads(path.read_text())
        payload["state"]["node_drops"] = {"/m/a.mkv": 1}
        path.write_text(json.dumps(payload))

        state = read_checkpoint(tmp_path, "j", reference)["state"]
        resumed = JobTracker(job_id="j", items=[], config=_config(), registry=MagicMock(), carried_state=state)

        assert resumed.node_drops == {"/m/a.mkv": [""]}

    @pytest.mark.parametrize(
        ("names", "why"),
        [
            (["", "N1"], "nodes N1 and an earlier one unavailable (each dropped the file once)"),
            (["", ""], "nodes unavailable (they dropped the file twice)"),
        ],
    )
    def test_a_drop_counted_without_its_node_still_reads_plainly(self, names, why):
        from media_preview_generator.jobs.dispatcher import _drops_failure

        assert _drops_failure(names) == why

    @pytest.mark.parametrize(
        ("field", "bad"),
        [
            ("node_drops", {"/m/a.mkv": "1"}),
            ("node_drops", {"/m/a.mkv": -1}),
            ("node_drops", {"/m/a.mkv": True}),
            ("node_drops", {"/m/a.mkv": [1]}),
            ("node_start_failures", {"/m/a.mkv": True}),
            ("dropped_on", {"/m/a.mkv": 5}),
            ("excluded_nodes", {"/m/a.mkv": "n2"}),
            ("excluded_nodes", {"/m/a.mkv": {"n2": 3}}),
            ("excluded_nodes", []),
        ],
    )
    def test_corrupt_node_counts_are_refused(self, tmp_path, field, bad):
        from media_preview_generator.jobs.checkpoints import write_checkpoint

        snapshot = self._parked_snapshot()
        snapshot["state"][field] = bad
        with pytest.raises(ValueError, match="node"):
            write_checkpoint(tmp_path, "j", snapshot)
