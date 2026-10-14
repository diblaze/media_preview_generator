"""Worker group members that run on a remote node: schema, runtime slots, capacity, parking and status rows."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from media_preview_generator.jobs import parking
from media_preview_generator.jobs.dispatcher import JobDispatcher, reset_dispatcher
from media_preview_generator.jobs.group_runtime import capacity_wait_message, refresh_worker_groups
from media_preview_generator.jobs.node_members import NODE_GPU_FFMPEG_THREADS, read_node_hardware, worker_node_id
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.nodes.registry import (
    STATE_GPU_LOST,
    STATE_OFFLINE,
    STATE_READY,
    NodeCapacity,
    NodeRegistry,
    NodeView,
    set_registry,
)
from media_preview_generator.worker_groups import (
    configured_group_totals,
    configured_node_totals,
    group_resource_key,
    member_policies,
    validate_worker_groups,
)

LOCAL_GPU = [("nvidia", "cuda:0", {"name": "Local RTX", "ffmpeg_threads": 3})]
NODE_GPU = {"type": "nvidia", "device": "cuda:0", "name": "Quadro P5000", "status": "ok"}
NODES = {"storage": "storage", "nuc": "nuc"}


class FakeRegistry(NodeRegistry):
    """In-memory registry: each node has a view and a capacity; ``revision`` bumps on every change."""

    def __init__(self) -> None:
        self.revision = 0
        self._views: dict[str, NodeView] = {}
        self._capacity: dict[str, NodeCapacity] = {}
        self.reads = 0

    def put(
        self, node_id: str, *, state: str = STATE_READY, gpus: tuple = (NODE_GPU,), reason: str = "", name=None
    ) -> None:
        self._views[node_id] = NodeView(
            id=node_id,
            name=name or node_id,
            url=f"http://{node_id}:9494",
            enabled=True,
            state=state,
            reason=reason,
            version="1",
            roles=("worker",),
            gpus=tuple(gpus),
            cpu_cores=24,
            legacy=False,
            last_seen=None,
        )
        ready = state == STATE_READY
        self._capacity[node_id] = NodeCapacity(cpu=ready or state == STATE_GPU_LOST, gpu=ready, reason=reason)
        self.revision += 1

    def remove(self, node_id: str) -> None:
        self._views.pop(node_id, None)
        self._capacity.pop(node_id, None)
        self.revision += 1

    def views(self) -> list[NodeView]:
        return list(self._views.values())

    def view(self, node_id: str) -> NodeView | None:
        self.reads += 1
        return self._views.get(node_id)

    def capacity(self, node_id: str) -> NodeCapacity:
        return self._capacity.get(node_id, NodeCapacity(False, False, "node removed"))


@pytest.fixture
def registry():
    fake = FakeRegistry()
    set_registry(fake)
    yield fake
    set_registry(None)


def member(mid, resource="cpu", device=None, count=1, kinds=None, node=None):
    raw = {
        "id": mid,
        "resource": resource,
        "device": device,
        "count": count,
        "job_types": kinds or (["previews", "intro_credits"] if resource == "gpu" else ["previews", "loudness"]),
    }
    if node is not None:
        raw["node"] = node
    return raw


def group(gid, *members, enabled=True):
    return {
        "id": gid,
        "name": gid,
        "enabled": enabled,
        "availability": {"mode": "always", "windows": []},
        "members": list(members),
    }


class TestSchema:
    def test_node_field_is_kept_for_node_members_and_absent_for_local_ones(self):
        clean = validate_worker_groups(
            [group("g", member("a"), member("b", "gpu", "cuda:0", node="storage"))], nodes=NODES
        )
        local, remote = clean[0]["members"]
        assert "node" not in local
        assert remote["node"] == "storage"

    def test_empty_node_means_this_app(self):
        clean = validate_worker_groups([group("g", member("a", node=""))], nodes=NODES)
        assert "node" not in clean[0]["members"][0]

    def test_unknown_node_is_refused_with_a_plain_message_when_saving(self):
        with pytest.raises(ValueError, match="node gone is not added. Add it under Nodes"):
            validate_worker_groups([group("g", member("a", node="gone"))], nodes=NODES)

    def test_unknown_node_is_kept_when_reading_saved_settings(self):
        clean = validate_worker_groups([group("g", member("a", node="gone"))])
        assert clean[0]["members"][0]["node"] == "gone"

    @pytest.mark.parametrize("bad", [5, " storage", "x" * 129])
    def test_malformed_node_is_refused(self, bad):
        with pytest.raises(ValueError, match="choose a node"):
            validate_worker_groups([group("g", member("a", node=bad))])

    def test_node_gpu_member_needs_a_device(self):
        with pytest.raises(ValueError, match="choose a GPU device"):
            validate_worker_groups([group("g", member("a", "gpu", None, node="storage"))], nodes=NODES)

    def test_loudness_on_a_node_gpu_is_refused_and_on_a_node_cpu_is_allowed(self):
        with pytest.raises(ValueError, match=r"storage · cuda:0\): Plex loudness requires CPU"):
            validate_worker_groups(
                [group("g", member("a", "gpu", "cuda:0", kinds=["loudness"], node="storage"))], nodes=NODES
            )
        clean = validate_worker_groups([group("g", member("a", kinds=["loudness"], node="storage"))], nodes=NODES)
        assert clean[0]["members"][0]["job_types"] == ["loudness"]

    def test_duplicates_are_keyed_per_node(self):
        same_device_everywhere = group(
            "g",
            member("local-cpu"),
            member("local-gpu", "gpu", "cuda:0"),
            member("storage-cpu", node="storage"),
            member("storage-gpu", "gpu", "cuda:0", node="storage"),
            member("nuc-cpu", node="nuc"),
        )
        assert len(validate_worker_groups([same_device_everywhere], nodes=NODES)[0]["members"]) == 5
        with pytest.raises(ValueError, match="storage · cuda:0 appears twice"):
            validate_worker_groups(
                [
                    group(
                        "g", member("a", "gpu", "cuda:0", node="storage"), member("b", "gpu", "cuda:0", node="storage")
                    )
                ],
                nodes=NODES,
            )
        with pytest.raises(ValueError, match="storage · CPU appears twice"):
            validate_worker_groups(
                [group("g", member("a", node="storage"), member("b", node="storage"))],
                nodes=NODES,
            )

    def test_node_workers_have_their_own_peak_cap_not_the_apps(self):
        groups = [
            group("local", member("a", count=64)),
            group("storage", member("a", count=64, node="storage")),
            group("nuc", member("a", count=64, node="nuc")),
        ]
        clean = validate_worker_groups(groups, nodes=NODES)
        assert configured_group_totals(clean) == (0, 64)
        assert configured_node_totals(clean) == {"storage": (0, 64), "nuc": (0, 64)}
        groups.append(group("more", member("a", count=1, node="storage")))
        with pytest.raises(ValueError, match="exceed capacity on node storage: peak CPU 65/64"):
            validate_worker_groups(groups, nodes=NODES)

    def test_member_policies_copy_node_and_resource_keys_never_collide(self):
        policies = member_policies(
            [
                group(
                    "g",
                    member("a"),
                    member("b", "gpu", "cuda:0"),
                    member("c", node="storage"),
                    member("d", "gpu", "cuda:0", node="storage"),
                    member("e", node="nuc"),
                )
            ]
        )
        assert [p["node"] for p in policies] == [None, None, "storage", "storage", "nuc"]
        assert [group_resource_key(p) for p in policies] == [
            "cpu",
            "gpu:cuda:0",
            "node:storage:cpu",
            "node:storage:gpu:cuda:0",
            "node:nuc:cpu",
        ]


def node_groups(*, gpu_count=2, cpu_count=1, node="storage"):
    return [
        group(
            "night",
            member("ngpu", "gpu", "cuda:0", count=gpu_count, node=node),
            member("ncpu", count=cpu_count, kinds=["previews", "loudness"], node=node),
            member("lgpu", "gpu", "cuda:0", count=1),
        )
    ]


def by_member(pool):
    rows: dict[str, list] = {}
    for worker in pool.workers:
        rows.setdefault(worker.member_id, []).append(worker)
    return rows


class TestReconcile:
    def test_ready_node_gets_workers_with_its_own_gpu_and_resource_keys(self, registry):
        registry.put("storage", name="Storage box")
        pool = WorkerPool(0, 0, LOCAL_GPU)
        pool.reconcile_groups(node_groups(), LOCAL_GPU)
        rows = by_member(pool)
        assert len(rows["ngpu"]) == 2 and len(rows["ncpu"]) == 1 and len(rows["lgpu"]) == 1
        gpu_worker = rows["ngpu"][0]
        assert (gpu_worker.worker_type, gpu_worker.gpu, gpu_worker.gpu_device, gpu_worker.gpu_name) == (
            "GPU",
            "nvidia",
            "cuda:0",
            "Quadro P5000",
        )
        assert (gpu_worker.node_id, gpu_worker.node_name) == ("storage", "Storage box")
        assert gpu_worker.ffmpeg_threads == NODE_GPU_FFMPEG_THREADS
        assert gpu_worker.group_resource == "node:storage:gpu:cuda:0"
        assert rows["ncpu"][0].group_resource == "node:storage:cpu"
        assert (rows["ncpu"][0].worker_type, rows["ncpu"][0].node_id) == ("CPU", "storage")
        local = rows["lgpu"][0]
        assert (worker_node_id(local), local.group_resource, local.ffmpeg_threads) == (None, "gpu:cuda:0", 3)
        assert pool._resource_targets == {"node:storage:gpu:cuda:0": 2, "node:storage:cpu": 1, "gpu:cuda:0": 1}

    def test_node_gpu_with_the_same_device_string_does_not_share_the_local_budget(self, registry):
        registry.put("storage")
        pool = WorkerPool(0, 0, LOCAL_GPU)
        pool.reconcile_groups(node_groups(gpu_count=1), LOCAL_GPU)
        local = pool._find_available_worker(kind="previews", claim=True)
        remote = pool._find_available_worker(kind="previews", claim=True)
        assert {local.group_resource, remote.group_resource} == {"gpu:cuda:0", "node:storage:gpu:cuda:0"}

    def test_offline_node_gives_zero_workers_with_its_reason(self, registry):
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        pool = WorkerPool(0, 0, LOCAL_GPU)
        pool.reconcile_groups(node_groups(), LOCAL_GPU)
        assert set(by_member(pool)) == {"lgpu"}
        rows = {row["member_id"]: row for row in pool.member_snapshots()}
        assert (rows["ngpu"]["state"], rows["ngpu"]["reason"], rows["ngpu"]["target"]) == (
            "node_unavailable",
            "node offline",
            0,
        )
        assert (rows["ncpu"]["state"], rows["ncpu"]["reason"]) == ("node_unavailable", "node offline")
        assert rows["ngpu"]["node_name"] == "storage"
        assert (rows["lgpu"]["state"], rows["lgpu"]["reason"], rows["lgpu"]["node_name"]) == ("active", "", None)

    def test_gpu_lost_keeps_only_the_nodes_cpu_members(self, registry):
        registry.put("storage", state=STATE_GPU_LOST, reason="GPU lost")
        pool = WorkerPool(0, 0, LOCAL_GPU)
        pool.reconcile_groups(node_groups(), LOCAL_GPU)
        assert set(by_member(pool)) == {"ncpu", "lgpu"}
        rows = {row["member_id"]: row for row in pool.member_snapshots()}
        assert (rows["ngpu"]["state"], rows["ngpu"]["reason"]) == ("node_unavailable", "GPU lost")
        assert rows["ncpu"]["state"] == "active"

    def test_node_gpu_missing_from_the_nodes_list_gets_zero(self, registry):
        registry.put("storage", gpus=({**NODE_GPU, "device": "cuda:1"},))
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups(node_groups(), [])
        rows = {row["member_id"]: row for row in pool.member_snapshots()}
        assert (rows["ngpu"]["state"], rows["ngpu"]["reason"]) == ("node_unavailable", "GPU not found on this node")
        assert rows["lgpu"]["state"] == "hardware_unavailable"

    def test_failed_node_gpu_gets_zero(self, registry):
        registry.put("storage", gpus=({**NODE_GPU, "status": "failed"},))
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups(node_groups(), [])
        assert "ngpu" not in by_member(pool)

    def test_removed_node_reads_as_node_removed(self, registry):
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups(node_groups(), [])
        rows = {row["member_id"]: row for row in pool.member_snapshots()}
        assert (rows["ncpu"]["state"], rows["ncpu"]["reason"]) == ("node_unavailable", "node removed")

    def test_node_going_offline_retires_idle_slots_and_finishes_busy_ones(self, registry):
        registry.put("storage")
        pool = WorkerPool(0, 0, [])
        groups = [group("g", member("ncpu", count=2, node="storage"))]
        pool.reconcile_groups(groups, [])
        busy, idle = pool.workers
        busy.is_busy = True
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        pool.reconcile_groups(groups, [])
        assert pool.workers == [busy] and busy._pending_removal
        registry.put("storage")
        pool.reconcile_groups(groups, [])
        assert busy in pool.workers and not busy._pending_removal and len(pool.workers) == 2

    def test_registry_failure_on_one_node_leaves_local_workers_running(self, registry, monkeypatch):
        def broken(node_id):
            raise RuntimeError("registry down")

        monkeypatch.setattr(registry, "capacity", broken)
        pool = WorkerPool(0, 0, LOCAL_GPU)
        pool.reconcile_groups(node_groups(), LOCAL_GPU)
        assert set(by_member(pool)) == {"lgpu"}
        rows = {row["member_id"]: row for row in pool.member_snapshots()}
        assert rows["ncpu"]["reason"] == "node status unavailable"

    def test_local_only_groups_never_read_the_registry(self, registry):
        pool = WorkerPool(0, 0, LOCAL_GPU)
        pool.reconcile_groups([group("g", member("a"), member("b", "gpu", "cuda:0"))], LOCAL_GPU)
        assert registry.reads == 0
        assert read_node_hardware(member_policies([group("g", member("a"))])).revision is None


class TestRevisionRefresh:
    @pytest.fixture
    def saved(self, monkeypatch):
        state = {"groups": node_groups(), "revision": 1}
        monkeypatch.setattr(
            "media_preview_generator.jobs.group_runtime.current_group_policy",
            lambda config=None: (state["groups"], state["revision"]),
        )
        return state

    def test_registry_revision_bump_reconciles_inside_the_one_second_throttle(self, registry, saved):
        registry.put("storage")
        pool = WorkerPool(0, 0, [])
        assert refresh_worker_groups(pool, force=True)
        assert {"ngpu", "ncpu"} <= set(by_member(pool))
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        assert refresh_worker_groups(pool)
        assert not ({"ngpu", "ncpu"} & set(by_member(pool)))

    def test_unchanged_revision_stays_throttled(self, registry, saved, monkeypatch):
        registry.put("storage")
        pool = WorkerPool(0, 0, [])
        refresh_worker_groups(pool, force=True)
        calls = []
        monkeypatch.setattr(pool, "reconcile_groups", lambda *a, **k: calls.append(1))
        refresh_worker_groups(pool)
        assert calls == []

    def test_revision_bump_is_ignored_when_no_member_uses_a_node(self, registry, saved, monkeypatch):
        saved["groups"] = [group("g", member("a"))]
        pool = WorkerPool(0, 0, [])
        refresh_worker_groups(pool, force=True)
        calls = []
        monkeypatch.setattr(pool, "reconcile_groups", lambda *a, **k: calls.append(1))
        registry.put("storage")
        refresh_worker_groups(pool)
        assert calls == []


class TestCapacityAndParking:
    def test_all_capacity_on_an_offline_node_reads_node_unavailable(self, registry):
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups([group("g", member("ncpu", count=3, node="storage"))], [])
        capacity = pool.capacity_for("previews")
        assert (capacity["configured"], capacity["open"], capacity["reason"]) == (3, 0, "node_unavailable")
        assert capacity["detail"] == "storage: node offline"
        assert capacity_wait_message(capacity) == "Waiting for a node — storage: node offline"

    def test_mixed_local_and_offline_node_keeps_the_hardware_reason(self, registry):
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups(node_groups(), [])
        capacity = pool.capacity_for("previews")
        assert (capacity["open"], capacity["reason"], capacity["detail"]) == (0, "hardware", "")

    def test_ready_node_capacity_is_open(self, registry):
        registry.put("storage")
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups([group("g", member("ncpu", count=3, node="storage"))], [])
        capacity = pool.capacity_for("loudness")
        assert (capacity["open"], capacity["available"], capacity["reason"]) == (3, 3, "ready")

    def test_job_parks_when_its_only_capacity_is_on_an_offline_node(self, registry, monkeypatch):
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups([group("g", member("ncpu", count=2, node="storage"))], [])
        monkeypatch.setattr(parking, "refresh_worker_groups", lambda pool: True)
        dispatcher = MagicMock()
        dispatcher.worker_pool = pool
        dispatcher.park_snapshot.return_value = None
        parking.park_if_unavailable(dispatcher, MagicMock(park_requested=False), MagicMock(), "job-1", "previews", dict)
        dispatcher.request_park.assert_called_once_with("job-1")

    def test_job_keeps_running_when_the_node_is_ready(self, registry, monkeypatch):
        registry.put("storage")
        pool = WorkerPool(0, 0, [])
        pool.reconcile_groups([group("g", member("ncpu", count=2, node="storage"))], [])
        monkeypatch.setattr(parking, "refresh_worker_groups", lambda pool: True)
        dispatcher = MagicMock()
        dispatcher.worker_pool = pool
        parking.park_if_unavailable(dispatcher, MagicMock(park_requested=False), MagicMock(), "job-1", "previews", dict)
        dispatcher.request_park.assert_not_called()


class TestWorkerStatuses:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_dispatcher()
        yield
        reset_dispatcher()

    def test_node_workers_are_named_after_their_node_and_carry_a_node_field(self, registry):
        registry.put("storage", name="Storage box")
        pool = WorkerPool(0, 0, LOCAL_GPU)
        pool.reconcile_groups(node_groups(), LOCAL_GPU)
        statuses = {(s["member_id"], s["worker_name"]): s for s in JobDispatcher(pool)._build_worker_statuses()}
        names = sorted(name for _, name in statuses)
        assert names == [
            "GPU Worker 1 (Local RTX)",
            "Storage box · CPU Worker 1",
            "Storage box · GPU Worker 1 (Quadro P5000)",
            "Storage box · GPU Worker 2 (Quadro P5000)",
        ]
        remote = statuses[("ngpu", "Storage box · GPU Worker 1 (Quadro P5000)")]
        assert remote["node"] == {"id": "storage", "name": "Storage box"}
        assert remote["node_state"] == "ready"
        assert remote["group_resource"] == "node:storage:gpu:cuda:0"
        local = statuses[("lgpu", "GPU Worker 1 (Local RTX)")]
        assert (local["node"], local["node_state"]) == (None, None)

    def test_busy_worker_on_a_node_that_went_offline_reports_the_state(self, registry):
        registry.put("storage")
        pool = WorkerPool(0, 0, [])
        groups = [group("g", member("ncpu", count=1, node="storage"))]
        pool.reconcile_groups(groups, [])
        pool.workers[0].is_busy = True
        registry.put("storage", state=STATE_OFFLINE, reason="node offline")
        pool.reconcile_groups(groups, [])
        (status,) = JobDispatcher(pool)._build_worker_statuses()
        assert (status["node_state"], status["retiring"]) == ("offline", True)
