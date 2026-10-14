"""A worker placed on a remote node: its heavy steps run there, and a file the node can't run goes back to the queue."""

from __future__ import annotations

import itertools
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import ItemOutcome
from media_preview_generator.jobs import worker as worker_mod
from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.nodes.executor import (
    REQUEUE_NODE_UNAVAILABLE,
    REQUEUE_NOT_VISIBLE,
    FileNotVisibleError,
    NodeUnavailableError,
    Placement,
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
from media_preview_generator.processing import CodecNotSupportedError
from media_preview_generator.processing.types import ProcessableItem
from tests.conftest import _ms, _pi

DEVICE = "/dev/dri/renderD128"


def _node_gpu_worker() -> Worker:
    return Worker(0, "GPU", "INTEL", DEVICE, 0, "Arc A380", node_id="n1", node_name="NAS")


def _run_preview(worker: Worker, side_effect) -> None:
    with patch("media_preview_generator.processing.multi_server.process_canonical_path", side_effect=side_effect):
        worker.assign_task(_pi("k", title="Film", media_type="movie"), MagicMock(cpu_threads=1), MagicMock())
        worker.current_thread.join(timeout=5)


def _gone() -> NodeUnavailableError:
    return NodeUnavailableError("n1", "NAS", "NAS stopped answering")


def _not_visible() -> FileNotVisibleError:
    return FileNotVisibleError("n1", "NAS", "missing", "no mapping covers /m/t.mkv")


class TestPlacement:
    def test_a_node_worker_has_its_own_slot_resource_and_placement(self):
        worker = _node_gpu_worker()
        assert worker.placement == Placement("n1", "NAS")
        assert worker.group_resource == f"node:n1:gpu:{DEVICE}"
        assert Worker(1, "CPU", node_id="n1").group_resource == "node:n1:cpu"

    def test_a_local_worker_is_unchanged(self):
        worker = Worker(0, "GPU", "NVIDIA", "cuda", 0, "RTX")
        assert worker.placement is None
        assert worker.group_resource == "gpu:cuda"
        assert worker.node_name == ""


class TestPreviewOnANode:
    def test_the_whole_item_runs_placed_on_the_node(self):
        seen = []

        def process(*args, **kwargs):
            seen.append((current_placement(), kwargs.get("gpu_device_path")))
            return _ms("generated")

        worker = _node_gpu_worker()
        item = _pi("k", title="Film")
        with patch("media_preview_generator.processing.multi_server.process_canonical_path", side_effect=process):
            worker.assign_task(item, MagicMock(cpu_threads=1), MagicMock())
            worker.current_thread.join(timeout=5)
        assert seen == [(Placement("n1", "NAS"), DEVICE)]
        assert worker.completed == 1
        assert worker.current_item is item, "the dispatcher reads the finished item back to queue it again"
        assert worker.last_task_requeue is None
        assert current_placement() is None, "the placement leaked out of the worker thread"

    def test_a_gpu_failure_reruns_on_the_same_nodes_cpu_and_keeps_the_nodes_own_streak(self):
        seen = []

        def process(*args, **kwargs):
            seen.append((current_placement(), kwargs.get("gpu")))
            if len(seen) == 1:
                raise CodecNotSupportedError("GPU processing failed", kind="hwaccel")
            return _ms("generated")

        worker = _node_gpu_worker()
        tracker = MagicMock()
        with patch("media_preview_generator.jobs.worker.get_gpu_fallback_tracker", return_value=tracker):
            _run_preview(worker, process)
        assert seen == [(Placement("n1", "NAS"), "INTEL"), (Placement("n1", "NAS"), None)]
        assert worker.completed == 1 and worker.fallback_active is True
        device, label = tracker.record.call_args.args
        assert device == f"node:n1:{DEVICE}"
        assert label.startswith("NAS ")

    @pytest.mark.parametrize(
        ("exc", "reason"),
        [(_gone, REQUEUE_NODE_UNAVAILABLE), (_not_visible, REQUEUE_NOT_VISIBLE)],
        ids=["node-gone", "not-visible"],
    )
    @pytest.mark.parametrize("on_cpu_rerun", [False, True], ids=["first-run", "cpu-rerun"])
    def test_a_file_the_node_cant_run_is_handed_back_without_an_outcome(self, exc, reason, on_cpu_rerun):
        calls = []

        def process(*args, **kwargs):
            calls.append(kwargs.get("gpu"))
            if on_cpu_rerun and len(calls) == 1:
                raise CodecNotSupportedError("GPU processing failed", kind="hwaccel")
            raise exc()

        worker = _node_gpu_worker()
        tracker = MagicMock()
        with (
            patch("media_preview_generator.jobs.worker._notify_file_result") as persisted,
            patch("media_preview_generator.jobs.worker.get_gpu_fallback_tracker", return_value=tracker),
        ):
            _run_preview(worker, process)
        assert len(calls) == (2 if on_cpu_rerun else 1)
        requeue = worker.last_task_requeue
        assert (requeue.reason, requeue.node_id, requeue.node_name) == (reason, "n1", "NAS")
        assert (worker.completed, worker.failed) == (0, 0)
        assert sum(worker.outcome_counts.values()) == 0
        persisted.assert_not_called()
        tracker.record.assert_not_called()

    def test_the_next_task_clears_the_hand_back(self):
        worker = _node_gpu_worker()
        _run_preview(worker, lambda *a, **k: (_ for _ in ()).throw(_gone()))
        assert worker.last_task_requeue is not None
        assert worker.check_completion() is True
        _run_preview(worker, lambda *a, **k: _ms("generated"))
        assert worker.last_task_requeue is None
        assert worker.completed == 1


class TestCustomKindOnANode:
    @staticmethod
    def _run(worker: Worker, process_fn) -> None:
        with patch("media_preview_generator.jobs.worker._notify_file_result") as persisted:
            worker.assign_task(
                ProcessableItem(canonical_path="/m/t.mkv", server_id="s1", title="t"),
                MagicMock(cpu_threads=0),
                MagicMock(),
                job_id="j",
                process_fn=process_fn,
                outcome_keys=("markers_published", "failed"),
            )
            worker.current_thread.join(timeout=5)
        return persisted

    def test_the_kind_runs_placed_on_the_node_and_its_cpu_rerun_too(self):
        seen = []

        def process_fn(item, *, gpu, **_kwargs):
            seen.append((current_placement(), gpu))
            if gpu is not None:
                raise CodecNotSupportedError("GPU processing failed", kind="hwaccel")
            return ItemOutcome("markers_published")

        worker = _node_gpu_worker()
        with patch("media_preview_generator.jobs.worker.get_gpu_fallback_tracker"):
            self._run(worker, process_fn)
        assert seen == [(Placement("n1", "NAS"), "INTEL"), (Placement("n1", "NAS"), None)]
        assert worker.outcome_counts.get("markers_published") == 1

    @pytest.mark.parametrize("on_cpu_rerun", [False, True], ids=["first-run", "cpu-rerun"])
    def test_a_file_the_node_cant_see_is_handed_back_without_an_outcome(self, on_cpu_rerun):
        def process_fn(item, *, gpu, **_kwargs):
            if on_cpu_rerun and gpu is not None:
                raise CodecNotSupportedError("GPU processing failed", kind="hwaccel")
            raise _not_visible()

        worker = _node_gpu_worker()
        persisted = self._run(worker, process_fn)
        assert worker.last_task_requeue.reason == REQUEUE_NOT_VISIBLE
        assert worker.current_item.canonical_path == "/m/t.mkv"
        assert (worker.completed, worker.failed) == (0, 0)
        assert sum(worker.outcome_counts.values()) == 0
        persisted.assert_not_called()


class TestPoolExcludesNodes:
    @staticmethod
    def _pool(*workers: Worker) -> WorkerPool:
        pool = WorkerPool(gpu_workers=0, cpu_workers=0, selected_gpus=[])
        pool.workers.extend(workers)
        return pool

    def test_a_node_that_cant_see_the_file_is_skipped_and_this_apps_workers_never_are(self):
        local = Worker(0, "CPU")
        on_a = Worker(1, "CPU", node_id="a", node_name="A")
        on_b = Worker(2, "CPU", node_id="b", node_name="B")
        assert self._pool(on_a, on_b)._find_available_worker(exclude_nodes={"a"}) is on_b
        assert self._pool(on_a)._find_available_worker(exclude_nodes={"a"}) is None
        assert self._pool(local, on_a)._find_available_worker(exclude_nodes={"a", "b"}) is local

    def test_can_run_outside_is_false_only_when_every_slot_is_on_an_excluded_node(self):
        on_a = Worker(1, "CPU", node_id="a")
        on_b = Worker(2, "CPU", node_id="b")
        assert self._pool(on_a, on_b).can_run_outside("previews", {"a"}) is True
        assert self._pool(on_a, on_b).can_run_outside("previews", {"a", "b"}) is False
        assert self._pool(on_a, Worker(0, "CPU")).can_run_outside("previews", {"a"}) is True
        assert self._pool().can_run_outside("previews", {"a"}) is True


class _Registry(NodeRegistry):
    """One view per node: its state and when the registry last heard from it."""

    revision = 0  # shadows the real registry's property

    def __init__(self) -> None:
        self.states: dict[str, tuple[str, float | None]] = {}
        self._clock = itertools.count(1)

    def set(self, node_id: str, state: str, *, seen_now: bool) -> None:
        last = self.states.get(node_id, (STATE_READY, None))[1]
        self.states[node_id] = (state, float(next(self._clock)) if seen_now else last)

    def view(self, node_id: str) -> NodeView | None:
        if node_id not in self.states:
            return None
        state, seen = self.states[node_id]
        return NodeView(node_id, node_id, "http://n", True, state, "", "1", ("worker",), (), 4, False, seen)

    def capacity(self, node_id: str) -> NodeCapacity:
        ready = self.states.get(node_id, (STATE_OFFLINE, None))[0] == STATE_READY
        return NodeCapacity(ready, ready, "" if ready else "node offline")


# Noon on a Monday; a scheduled group open only 00:00-00:01 is outside its hours then.
NOON = datetime(2026, 10, 12, 12, 0)
OFF_HOURS = {"mode": "scheduled", "windows": [{"days": list(range(7)), "start": "00:00", "end": "00:01"}]}
ALWAYS = {"mode": "always", "windows": []}


def _group(gid: str, *, node: str | None = None, kinds=("previews",), availability=ALWAYS, enabled=True) -> dict:
    member = {"id": "m1", "resource": "cpu", "device": None, "count": 1, "job_types": list(kinds)}
    if node is not None:
        member["node"] = node
    return {"id": gid, "name": gid, "enabled": enabled, "availability": availability, "members": [member]}


class TestCanRunOutsideWithGroups:
    """Every enabled member that supports the kind counts, whatever its hours or its node's state right now: the file
    waits for it instead of failing."""

    @pytest.fixture(autouse=True)
    def registry(self):
        fake = _Registry()
        fake.set("a", STATE_READY, seen_now=True)
        fake.set("b", STATE_OFFLINE, seen_now=False)
        set_registry(fake)
        yield fake
        set_registry(None)

    @staticmethod
    def _can_run_outside_a(*groups: dict) -> bool:
        pool = WorkerPool(gpu_workers=0, cpu_workers=0, selected_gpus=[])
        pool.reconcile_groups([_group("nas", node="a"), *groups], [], now=NOON)
        return pool.can_run_outside("previews", {"a"})

    @pytest.mark.parametrize(
        ("other", "expected"),
        [
            (_group("local", availability=OFF_HOURS), True),
            (_group("other-node", node="b"), True),
            (_group("other-node-off-hours", node="b", availability=OFF_HOURS), True),
            (_group("local"), True),
            (_group("local", enabled=False), False),
            (_group("local", kinds=("loudness",)), False),
            (_group("nas-night", node="a", availability=OFF_HOURS), False),
            (None, False),
        ],
        ids=[
            "this-app-off-hours",
            "offline-node",
            "offline-node-off-hours",
            "this-app-open",
            "this-app-disabled",
            "this-app-other-kind",
            "excluded-node-off-hours",
            "nothing-else",
        ],
    )
    def test_members_that_count(self, other, expected):
        assert self._can_run_outside_a(*([other] if other else [])) is expected


class TestSuspectNodes:
    @pytest.fixture
    def registry(self):
        fake = _Registry()
        fake.set("a", STATE_READY, seen_now=True)
        set_registry(fake)
        yield fake
        set_registry(None)

    @staticmethod
    def _pool() -> tuple[WorkerPool, Worker, Worker]:
        on_a = Worker(0, "GPU", "NVIDIA", "cuda:0", 0, "RTX", node_id="a", node_name="A")
        local = Worker(1, "CPU")
        pool = WorkerPool(gpu_workers=0, cpu_workers=0, selected_gpus=[])
        pool.workers.extend([on_a, local])
        return pool, on_a, local

    def test_a_node_that_dropped_a_file_gets_none_until_its_next_check_comes_back_ready(self, registry):
        pool, on_a, local = self._pool()
        assert pool._find_available_worker() is on_a  # an idle GPU wins
        pool.mark_node_suspect("a")
        assert pool._find_available_worker() is local
        registry.set("a", STATE_OFFLINE, seen_now=False)  # the re-check: still down
        pool.refresh_suspect_nodes()
        assert pool._find_available_worker() is local
        registry.set("a", STATE_READY, seen_now=False)  # ready, but nobody has heard from it since the drop
        pool.refresh_suspect_nodes()
        assert pool._find_available_worker() is local
        registry.set("a", STATE_READY, seen_now=True)
        pool.refresh_suspect_nodes()
        assert pool._find_available_worker() is on_a

    def test_the_hold_ends_on_its_own_after_a_while(self, registry, monkeypatch):
        pool, on_a, local = self._pool()
        monkeypatch.setattr(worker_mod, "SUSPECT_NODE_S", 0.0)
        pool.mark_node_suspect("a")
        assert pool._find_available_worker() is on_a
        pool.refresh_suspect_nodes()
        assert pool._suspect_nodes == {}

    def test_only_the_dropping_nodes_workers_are_held(self, registry):
        pool, on_a, local = self._pool()
        on_b = Worker(2, "GPU", "NVIDIA", "cuda:0", 0, "RTX", node_id="b", node_name="B")
        pool.workers.append(on_b)
        pool.mark_node_suspect("a")
        assert pool._find_available_worker() is on_b
