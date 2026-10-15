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
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker, StartWait, reset_dispatcher
from media_preview_generator.jobs.node_members import NodeHardware
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.client import NodeUnreachableError
from media_preview_generator.nodes.executor import (
    REQUEUE_NODE_NOT_STARTED,
    REQUEUE_NODE_UNAVAILABLE,
    FileNotVisibleError,
    NodeRequeue,
    NodeUnavailableError,
    current_placement,
)
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
from tests.nodes.test_registry import Harness

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

    def work_waiting(self, node_ids) -> None:
        pass

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


@pytest.fixture(autouse=True)
def _quick_start_retries(monkeypatch):
    # A file whose start a node failed waits a second (doubling) before it goes back there; tests that don't look at
    # that wait keep it short.
    monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_S", 0.01)


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


def _offline_on_its_back_off() -> Harness:
    """A real registry whose node "n1" dropped a file and then stayed down for minutes: next checked 300 s out."""
    h = Harness()
    h.registry.check_now("n1")
    h.client().default_info = NodeUnreachableError("http://nuc:9494 didn't answer (no connection)")
    h.registry._run_check = lambda *args, **kwargs: None  # keep the report's own check out of it
    h.registry.report_unavailable("n1")
    del h.registry._run_check
    for _ in range(len(reg.OFFLINE_BACKOFF_S)):
        h.registry.check_now("n1")
        h.clock.now = h.registry._nodes["n1"].next_check
    h.registry.check_now("n1")
    assert h.registry._nodes["n1"].next_check - h.clock.now == reg.OFFLINE_BACKOFF_S[-1]
    set_registry(h.registry)
    return h


def _next_check_in(h: Harness) -> float:
    return h.registry._nodes["n1"].next_check - h.clock.now


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
    def test_a_dropped_file_waits_for_the_busy_worker_elsewhere_until_the_node_that_dropped_it_checks_ready(
        self, registry, other, other_name
    ):
        """Back on a node that hasn't answered since it dropped the file, a second drop would fail it, though the
        healthy worker that's busy now runs it a moment later."""
        registry.fresh = False
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

        tracker, rows = _run(_pool((other, other_name), ("a", "A")), process, "/m/long.mkv", "/m/x.mkv")

        assert [node for path, node in calls if path == "/m/x.mkv"] == ["a", other]
        assert [r.args[1].value for r in rows if r.args[1].value == "failed"] == []
        assert (tracker.successful, tracker.failed) == (2, 0)
        assert tracker.active_processing == 0

    def test_a_dropped_file_goes_back_to_its_node_once_it_checks_ready_rather_than_wait_for_a_busy_worker(
        self, registry
    ):
        """This app's one worker holds a file frozen by its job's schedule (or a long film) for hours; node A drops
        another job's only file once (a restart), checks ready at once and sits idle. The file runs on A again."""
        registry.fresh = True
        frozen = threading.Event()
        tries: list[str | None] = []
        lock = threading.Lock()

        def frozen_job(item, **_kwargs):
            frozen.wait(30)
            return ItemOutcome("markers_published", "ok")

        def webhook_job(item, **_kwargs):
            with lock:
                tries.append(_node())
                first = len(tries) == 1
            if first:
                raise NodeUnavailableError("a", "A", "A stopped answering")
            return ItemOutcome("markers_published", "ok")

        def handlers(process) -> KindHandlers:
            return KindHandlers(check_fn=lambda item, *, cancel_check: None, process_fn=process, outcome_keys=KEYS)

        pool = _pool((None, ""), ("a", "A"))
        dispatcher = JobDispatcher(pool)
        try:
            with (
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
            ):
                dispatcher.submit_items(
                    "F", _items("/m/frozen.mkv"), _config(), MagicMock(), kind="intro_credits",
                    handlers=handlers(frozen_job),
                )  # fmt: skip
                deadline = time.monotonic() + 5
                while not pool.workers[0].is_busy and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert pool.workers[0].is_busy, "job F's file should hold this app's worker"
                webhook = dispatcher.submit_items(
                    "W", _items("/m/new-episode.mkv"), _config(), MagicMock(), kind="intro_credits",
                    handlers=handlers(webhook_job),
                )  # fmt: skip
                finished = webhook.wait(timeout=8)
        finally:
            frozen.set()
            dispatcher.shutdown()
        assert finished, f"the dropped file is still waiting (tried on {tries})"
        assert tries == ["a", "a"]
        assert (webhook.successful, webhook.failed) == (1, 0)

    def test_a_flapping_node_a_file_was_sent_back_to_doesnt_fail_it_while_a_worker_elsewhere_runs_its_kind(
        self, registry
    ):
        """A node that passes its idle checks but drops files under load: sent back to it because this app's worker
        was busy, the file dropped there a second time and failed, though this app's worker ran it a moment later."""
        registry.fresh = True
        second_try = threading.Event()
        tries: list[str | None] = []
        lock = threading.Lock()

        def frozen_job(item, **_kwargs):
            second_try.wait(10)
            return ItemOutcome("markers_published", "ok")

        def flapping(item, **_kwargs):
            node = _node()
            with lock:
                tries.append(node)
                if len(tries) >= 2:
                    second_try.set()
            if node == "a":
                raise NodeUnavailableError("a", "A", "A stopped answering")
            return ItemOutcome("markers_published", "ok")

        def handlers(process) -> KindHandlers:
            return KindHandlers(check_fn=lambda item, *, cancel_check: None, process_fn=process, outcome_keys=KEYS)

        pool = _pool((None, ""), ("a", "A"))
        dispatcher = JobDispatcher(pool)
        try:
            with (
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
            ):
                dispatcher.submit_items(
                    "F", _items("/m/long.mkv"), _config(), MagicMock(), kind="intro_credits",
                    handlers=handlers(frozen_job),
                )  # fmt: skip
                deadline = time.monotonic() + 5
                while not pool.workers[0].is_busy and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert pool.workers[0].is_busy, "job F's file should hold this app's worker"
                job = dispatcher.submit_items(
                    "W", _items("/m/x.mkv"), _config(), MagicMock(), kind="intro_credits", handlers=handlers(flapping)
                )
                finished = job.wait(timeout=10)
        finally:
            second_try.set()
            dispatcher.shutdown()
        assert finished, f"the file is still waiting (tried on {tries})"
        # Sent back once (b018d29c), dropped again: it waits for this app's worker instead of going back for good.
        assert tries == ["a", "a", None]
        assert (job.successful, job.failed) == (1, 0)

    def test_a_dropped_file_waiting_for_its_node_to_check_ready_says_so(self, registry):
        registry.fresh = False
        tries: list[str | None] = []
        messages: list[str] = []

        def process(item, **_kwargs):
            tries.append(_node())
            if len(tries) == 1:
                raise NodeUnavailableError("a", "A", "A stopped answering")
            return ItemOutcome("markers_published", "ok")

        pool = _pool(("a", "A"))
        pool._node_hardware = NodeHardware(views={"a": registry.view("a")})
        dispatcher = JobDispatcher(pool)
        try:
            with (
                patch("media_preview_generator.processing.generator._notify_file_result"),
                patch("media_preview_generator.jobs.worker._notify_file_result"),
                patch("media_preview_generator.web.jobs.get_job_manager"),
            ):
                tracker = dispatcher.submit_items(
                    "j1", _items("/m/x.mkv"), _config(), MagicMock(), kind="intro_credits",
                    handlers=KindHandlers(
                        check_fn=lambda item, *, cancel_check: None, process_fn=process, outcome_keys=KEYS
                    ),
                    callbacks={"progress_callback": lambda _done, _total, message, **_k: messages.append(message)},
                )  # fmt: skip
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and messages[-1:] != ["Waiting for a node — A: checking it again"]:
                    time.sleep(0.05)
                assert tracker.progress_message() == "Waiting for a node — A: checking it again", messages
                assert tries == ["a"]

                registry.back("a")
                assert tracker.wait(timeout=10)
        finally:
            dispatcher.shutdown()
        assert tries == ["a", "a"]
        assert tracker.progress_message() == "1/1 completed"

    def test_a_dropped_file_waiting_for_its_offline_node_gets_that_node_checked_soon(self):
        # The banner says the node is being checked; on its back-off, it wasn't for up to five more minutes.
        h = _offline_on_its_back_off()
        pool = _pool((None, "local"))
        pool.workers[0].is_busy = True  # runs the kind, busy (another job's file, frozen by its schedule)
        dispatcher = JobDispatcher(pool)
        tracker = TestClaimWorker._tracker("/m/x.mkv")
        tracker.kind = "intro_credits"
        tracker.dropped_on = {"/m/x.mkv": "n1"}
        try:
            assert dispatcher._node_wait(tracker) == "Waiting for a node — n1: checking it again"
        finally:
            dispatcher.shutdown()
            h.registry.stop()

        assert _next_check_in(h) <= reg.WAITING_RECHECK_S

    def test_a_dropped_file_passed_over_while_its_job_runs_others_gets_its_node_checked_soon(self):
        # Something of the job runs, so no banner: the dispatcher passing the file over has to bring the check forward.
        h = _offline_on_its_back_off()
        pool = _pool((None, "local"))
        pool.workers[0].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = TestClaimWorker._tracker("/m/x.mkv")
        tracker.kind = "intro_credits"
        tracker.active_processing = 1
        tracker.dropped_on = {"/m/x.mkv": "n1", "/m/done.mkv": "n2"}
        dispatcher._trackers["j"] = tracker
        try:
            dispatcher._assign_tasks()
        finally:
            dispatcher._trackers.clear()
            dispatcher.shutdown()
            h.registry.stop()

        assert [item.canonical_path for item in tracker.item_queue] == ["/m/x.mkv"]
        assert _next_check_in(h) <= reg.WAITING_RECHECK_S

    def test_a_node_whose_dropped_files_are_no_longer_queued_keeps_its_back_off(self):
        h = _offline_on_its_back_off()
        pool = _pool((None, "local"))
        pool.workers[0].is_busy = True
        dispatcher = JobDispatcher(pool)
        tracker = TestClaimWorker._tracker("/m/y.mkv")
        tracker.kind = "intro_credits"
        tracker.active_processing = 1
        tracker.dropped_on = {"/m/x.mkv": "n1"}  # ran elsewhere since
        dispatcher._trackers["j"] = tracker
        try:
            dispatcher._assign_tasks()
        finally:
            dispatcher._trackers.clear()
            dispatcher.shutdown()
            h.registry.stop()

        assert _next_check_in(h) == reg.OFFLINE_BACKOFF_S[-1]

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
        assert (
            row.args[2]
            == "node NAS unavailable (it couldn't start the file 3 times). Last try: NAS couldn't start the task"
        )
        assert tracker.node_failures == {"/m/a.mkv": row.args[2]}
        assert tracker.node_drops == {}
        assert tracker.active_processing == 0

    @pytest.mark.parametrize(
        ("cap", "waits"), [(30.0, [0.1, 0.2, 0.4]), (0.2, [0.1, 0.2, 0.2])], ids=["doubling", "capped"]
    )
    def test_a_node_whose_starts_fail_while_its_check_answers_gets_the_file_back_only_after_a_doubling_wait(
        self, monkeypatch, cap, waits
    ):
        """Its check reads only /v1/info, which answered while its starts failed (out of threads, a gateway's 5xx):
        the check vouched for it within milliseconds, and the file's five starts were spent in ~70 ms."""
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_S", 0.1)
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_MAX_S", cap)
        tries: list[tuple[str | None, float]] = []

        def process(item, **_kwargs):
            tries.append((_node(), time.monotonic()))
            if len(tries) <= len(waits):
                raise NodeUnavailableError("n1", "NAS", "NAS couldn't start the task", started=False)
            return ItemOutcome("markers_published", "ok")

        tracker, rows = _run(_pool(("n1", "NAS")), process, "/m/a.mkv")

        assert (tracker.successful, tracker.failed) == (1, 0), rows
        assert [node for node, _ in tries] == ["n1"] * (len(waits) + 1)
        gaps = [later - earlier for (_, earlier), (_, later) in itertools.pairwise(tries)]
        assert all(gap >= wait for gap, wait in zip(gaps, waits, strict=True)), gaps
        # Capped: the third wait is the cap, not the doubled 0.4 s (a wide margin for a loaded box).
        assert cap > 0.4 or gaps[2] < 0.35, gaps

    def test_a_file_the_node_cant_start_runs_on_this_apps_worker_once_it_is_free_rather_than_failing_there(
        self, monkeypatch
    ):
        """This app's one worker is busy with the job's other file for a moment; the node answers its check while
        every start fails. The file waited for neither and failed on the node within milliseconds."""
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_S", 0.3)
        done: list[tuple[str, str | None]] = []
        node_starts: list[str] = []
        lock = threading.Lock()

        def process(item, **_kwargs):
            node = _node()
            if node == "n1":
                with lock:
                    node_starts.append(item.canonical_path)
                raise NodeUnavailableError("n1", "NAS", "NAS couldn't start the task", started=False)
            time.sleep(0.5)
            with lock:
                done.append((item.canonical_path, node))
            return ItemOutcome("markers_published", "ok")

        tracker, rows = _run(_pool((None, ""), ("n1", "NAS")), process, "/m/a.mkv", "/m/b.mkv")

        assert (tracker.successful, tracker.failed) == (2, 0), rows
        assert sorted(done) == [("/m/a.mkv", None), ("/m/b.mkv", None)]
        assert len(node_starts) < dispatcher_mod.MAX_NODE_START_FAILURES
        assert tracker.active_processing == 0

    def test_a_file_the_node_cant_start_goes_to_a_free_worker_elsewhere_without_waiting(self, monkeypatch):
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_S", 10.0)
        calls = []

        def process(item, **_kwargs):
            calls.append(_node())
            if len(calls) == 1:
                raise NodeUnavailableError("n1", "NAS", "NAS couldn't start the task", started=False)
            return ItemOutcome("markers_published", "ok")

        started = time.monotonic()
        tracker, _ = _run(_pool(("n1", "NAS"), (None, ""), gpu_on=frozenset({"n1"})), process, "/m/a.mkv")

        assert calls == ["n1", None]
        assert (tracker.successful, tracker.failed) == (1, 0)
        assert time.monotonic() - started < 5.0

    def test_a_file_that_keeps_filling_a_nodes_temp_folder_fails_naming_it(self, monkeypatch):
        # It failed as only "it couldn't start the file 5 times": nothing said the temp folder was too small for it.
        monkeypatch.setattr(dispatcher_mod, "MAX_NODE_START_FAILURES", 2)
        said = (
            "NAS couldn't finish the task: The node's temp folder /scratch is full (No space left on device); a "
            "preview needs about twice its frames' size there"
        )

        def process(item, **_kwargs):
            raise NodeUnavailableError("n1", "NAS", said, started=False)

        _tracker, rows = _run(_pool(("n1", "NAS")), process, "/m/a.mkv")

        (row,) = rows
        assert "temp folder /scratch is full" in row.args[2], row.args[2]

    @staticmethod
    def _starts_fail_for(outage_s: float, tries: list, *, local_s: float = 0.0):
        """A process whose node starts fail (an HTTP 502 from a gateway in front) until ``outage_s`` after the first
        try; this app's worker takes ``local_s`` per file."""
        lock = threading.Lock()
        began: list[float] = []

        def process(item, **_kwargs):
            node = _node()
            now = time.monotonic()
            with lock:
                if not began:
                    began.append(now)
                tries.append((node, now - began[0]))
            if node is None:
                time.sleep(local_s)
            elif now - began[0] < outage_s:
                raise NodeUnavailableError(
                    node, node.upper(), f"{node} couldn't start the task: HTTP 502", started=False
                )
            return ItemOutcome("markers_published", "ok")

        return process

    @pytest.mark.parametrize(
        "nodes", [(("n1", "N1"),), (("n1", "N1"), ("n2", "N2"))], ids=["one-node", "two-nodes-behind-one-gateway"]
    )
    def test_a_file_survives_a_short_start_outage_on_every_node_not_only_on_one(self, monkeypatch, nodes):
        """Its waits (0.1 + 0.2 s) outlast the 0.25 s outage; with two nodes the file went n1 -> n2 -> n1 -> n2 -> n1
        within milliseconds, so all five starts were spent before either wait ran out."""
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_S", 0.1)
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_MAX_S", 30.0)
        tries: list = []

        tracker, rows = _run(_pool(*nodes), self._starts_fail_for(0.25, tries), "/m/a.mkv")

        assert (tracker.successful, tracker.failed) == (1, 0), (tries, [r.args[:3] for r in rows])
        assert tracker.node_failures == {}

    def test_a_start_outage_on_the_node_fails_no_file_while_this_apps_worker_takes_them_one_by_one(self, monkeypatch):
        """One node and this app's one worker, 20 files, the node's starts failing for longer than five waits: 12 of
        20 files failed on the node although nothing ran and this app's worker would have taken them."""
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_S", 0.02)  # five failures in 0.02+0.04+0.08+0.16 s
        tries: list = []
        paths = [f"/m/{n:02}.mkv" for n in range(20)]

        tracker, rows = _run(_pool((None, ""), ("n1", "N1")), self._starts_fail_for(0.6, tries, local_s=0.05), *paths)

        assert (tracker.successful, tracker.failed) == (20, 0), [r.args[:3] for r in rows]
        assert tracker.node_start_failures == {}
        assert any(node == "n1" and at >= 0.6 for node, at in tries)  # the node took files again once it was back
        assert tracker.active_processing == 0

    def test_a_file_no_node_can_ever_start_fails_soon_naming_every_node_that_failed_it(self):
        """Nothing else runs its kind: each start counts (the first, while the other node hadn't failed it yet,
        doesn't), so it ends after one try more than the limit."""
        tries: list = []
        started = time.monotonic()

        tracker, rows = _run(_pool(("a", "A"), ("b", "B")), self._starts_fail_for(3600.0, tries), "/m/a.mkv")

        assert time.monotonic() - started < 5.0
        assert len(tries) == dispatcher_mod.MAX_NODE_START_FAILURES + 1
        assert {node for node, _ in tries} == {"a", "b"}
        (row,) = rows
        names = " and ".join(node.upper() for node in dict.fromkeys(node for node, _ in tries))
        assert row.args[2] == (
            f"nodes {names} unavailable (none of them could start the file). "
            f"Last try: {tries[-1][0]} couldn't start the task: HTTP 502"
        )
        assert tracker.node_failures == {"/m/a.mkv": row.args[2]}
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

    def test_a_file_whose_start_its_node_failed_goes_back_there_only_after_its_wait_though_the_node_checked_ready(
        self,
    ):
        pool = _pool(("a", "A"))
        tracker = self._tracker("/m/1.mkv", "/m/2.mkv")
        tracker.dropped_on = {"/m/1.mkv": "a"}
        tracker.start_waits = {"/m/1.mkv": {"a": StartWait("A", 1, time.monotonic() + 60)}}
        dispatcher = JobDispatcher(pool)

        worker, item = dispatcher._claim_worker(tracker, [], back_nodes={"a"})
        assert (worker.node_id, item.canonical_path) == ("a", "/m/2.mkv")
        worker.is_busy = False
        assert dispatcher._claim_worker(tracker, [], back_nodes={"a"}) is None

        tracker.start_waits["/m/1.mkv"]["a"] = StartWait("A", 1, time.monotonic() - 1)
        worker, item = dispatcher._claim_worker(tracker, [], back_nodes={"a"})
        assert (worker.node_id, item.canonical_path) == ("a", "/m/1.mkv")

    def test_a_file_goes_to_no_node_whose_start_wait_hasnt_run_out_not_only_the_last_one_that_failed_it(self):
        """Two nodes behind one gateway fail their starts together: the file went n1 -> n2 -> n1 -> n2 -> n1 within
        milliseconds and spent its five starts before either wait ran out."""
        pool = _pool(("a", "A"), ("b", "B"))
        tracker = self._tracker("/m/1.mkv", "/m/2.mkv")
        tracker.dropped_on = {"/m/1.mkv": "b"}
        later = time.monotonic() + 60
        tracker.start_waits = {"/m/1.mkv": {"a": StartWait("A", 1, later), "b": StartWait("B", 1, later)}}
        dispatcher = JobDispatcher(pool)

        worker, item = dispatcher._claim_worker(tracker, [], back_nodes={"a", "b"})
        assert (worker.node_id, item.canonical_path) == ("a", "/m/2.mkv")
        assert dispatcher._claim_worker(tracker, [], back_nodes={"a", "b"}) is None  # b is free, but waited on
        worker.is_busy = False
        assert dispatcher._claim_worker(tracker, [], back_nodes={"a", "b"}) is None  # so is a

        tracker.start_waits["/m/1.mkv"]["a"] = StartWait("A", 1, time.monotonic() - 1)
        worker, item = dispatcher._claim_worker(tracker, [], back_nodes={"a", "b"})
        assert (worker.node_id, item.canonical_path) == ("a", "/m/1.mkv")

    def test_each_start_a_node_fails_doubles_the_files_wait_there_up_to_the_cap_and_leaves_other_nodes_waits(
        self, monkeypatch
    ):
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_S", 1.0)
        monkeypatch.setattr(dispatcher_mod, "NODE_START_RETRY_MAX_S", 3.0)
        pool = _pool(("a", "A"), ("b", "B"), ("c", "C"))
        tracker = self._tracker()
        dispatcher = JobDispatcher(pool)
        (item,) = _items("/m/1.mkv")

        def fail_start(node: str) -> float:
            before = time.monotonic()
            requeue = NodeRequeue(REQUEUE_NODE_NOT_STARTED, node, node.upper(), f"{node} couldn't start the task")
            dispatcher._requeue_or_fail(pool.workers[0], tracker, item, requeue)
            tracker.item_queue.clear()
            return round(tracker.start_waits["/m/1.mkv"][node].retry_at - before, 1)

        assert [fail_start("a") for _ in range(4)] == [1.0, 2.0, 3.0, 3.0]
        a_wait = tracker.start_waits["/m/1.mkv"]["a"]
        assert fail_start("b") == 1.0  # b's own first failure
        assert tracker.dropped_on == {"/m/1.mkv": "b"}

        dropped = NodeRequeue(REQUEUE_NODE_UNAVAILABLE, "c", "C", "C stopped answering")
        dispatcher._requeue_or_fail(pool.workers[2], tracker, item, dropped)
        assert tracker.dropped_on == {"/m/1.mkv": "c"}
        assert tracker.start_waits["/m/1.mkv"]["a"] == a_wait
        assert [(w.node_name, w.failures) for w in tracker.start_waits["/m/1.mkv"].values()] == [("A", 4), ("B", 1)]

    @pytest.mark.parametrize(
        ("others", "busy", "failed_before", "hidden_on", "counted"),
        [
            ((), False, (), (), 1),
            (((None, ""),), False, (), (), 0),
            # Busy or not: it takes the file once it's free.
            (((None, ""),), True, (), (), 0),
            ((("b", "B"),), False, (), (), 0),
            ((("b", "B"),), False, ("b",), (), 1),
            ((("b", "B"),), False, (), ("b",), 1),
        ],
        ids=[
            "only-that-node",
            "this-apps-worker",
            "this-apps-worker-busy",
            "a-node-that-hasnt-failed-it",
            "a-node-that-failed-it-too",
            "a-node-that-cant-see-it",
        ],
    )
    def test_a_failed_start_counts_toward_the_limit_only_once_no_worker_off_the_failing_nodes_runs_its_kind(
        self, others, busy, failed_before, hidden_on, counted
    ):
        pool = _pool(("a", "A"), *others)
        if busy:
            pool.workers[1].is_busy = True
        tracker = self._tracker()
        tracker.excluded_nodes = {"/m/1.mkv": {node: node.upper() for node in hidden_on}} if hidden_on else {}
        tracker.start_waits = {"/m/1.mkv": {node: StartWait(node.upper(), 1, 0.0) for node in failed_before}}
        (item,) = _items("/m/1.mkv")

        not_started = NodeRequeue(REQUEUE_NODE_NOT_STARTED, "a", "A", "A couldn't start the task")
        JobDispatcher(pool)._requeue_or_fail(pool.workers[0], tracker, item, not_started)

        assert tracker.node_start_failures.get("/m/1.mkv", 0) == counted
        assert [i.canonical_path for i in tracker.item_queue] == ["/m/1.mkv"]

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
