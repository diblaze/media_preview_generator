"""NodeRegistry: the node state machine, capacity, reports from job code and the health loop."""

from __future__ import annotations

import json
import time

import pytest

from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.access_results import AccessResultStore
from media_preview_generator.nodes.client import NodeAuthError, NodeProtocolError, NodeUnreachableError
from media_preview_generator.nodes.protocol import ROLE_PLEX_DB, ROLE_WORKER

APP_VERSION = "9.9.9"


def _info(uid="uid-1", version=APP_VERSION, roles=(ROLE_WORKER,), gpus=None, **extra):
    return {
        "ok": True,
        "node": {"id": uid, "name": "nuc", "version": version, "protocols": [1], "roles": list(roles)},
        "platform": {"os": "Linux", "arch": "x86_64", "python": "3.12"},
        "worker": {
            "gpus": gpus if gpus is not None else [],
            "cpu": {"cores": 8, "load1": 0.5, "ram_total": 16, "ram_available": 8},
            "busy": False,
        },
        **extra,
    }


GPU_OK = {"type": "NVIDIA", "device": "cuda:0", "name": "RTX", "status": "ok", "error": None}
GPU_BAD = {**GPU_OK, "status": "failed", "error": "driver gone"}


class FakeClient:
    """Answers ``info`` / ``rescan_gpus`` from scripted queues; an exception instance in a queue is raised."""

    def __init__(self, record):
        self.record = record
        self.infos: list = []
        self.default_info: object = _info()
        self.rescans: list = []
        self.info_calls = 0
        self.rescan_calls = 0

    def info(self):
        self.info_calls += 1
        answer = self.infos.pop(0) if self.infos else self.default_info
        if isinstance(answer, Exception):
            raise answer
        return answer

    def rescan_gpus(self):
        self.rescan_calls += 1
        answer = self.rescans.pop(0) if self.rescans else {"gpus": [GPU_OK]}
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Harness:
    def __init__(self, nodes=None, access_store=None):
        self.nodes = nodes if nodes is not None else [_record()]
        self.revision = 1
        self.clients: dict[str, FakeClient] = {}
        self.uids: list[tuple[str, str]] = []
        self.legacy = ("connected", "1.2.0")
        self.quick = True
        self.clock = Clock()
        self.registry = reg.NodeRegistry(
            revision_provider=lambda: self.revision,
            nodes_provider=lambda: [dict(n) for n in self.nodes],
            client_factory=self._client,
            legacy_ping=lambda url, key: self.legacy,
            record_uid=lambda node_id, uid: self.uids.append((node_id, uid)),
            quick_access=lambda client, mappings: self.quick,
            app_version=lambda: APP_VERSION,
            attempt_gap_s=0.0,
            clock=self.clock,
            wall_clock=lambda: 1_700_000_000.0,
            access_store=access_store,
        )

    def _client(self, record):
        client = FakeClient(record)
        self.clients[record["id"]] = client
        return client

    def client(self, node_id="n1") -> FakeClient:
        self.registry.views()  # builds the clients
        return self.clients[node_id]

    def change(self, **fields):
        self.nodes[0] = {**self.nodes[0], **fields}
        self.revision += 1


def _record(**fields):
    return {
        "id": "n1",
        "node_uid": None,
        "name": "nuc",
        "url": "http://nuc:9494",
        "key": "k",
        "enabled": True,
        "path_mappings": [{"app_prefix": "/data", "node_prefix": "/mnt"}],
        **fields,
    }


@pytest.fixture
def h():
    harness = Harness()
    yield harness
    harness.registry.stop()


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestStates:
    def test_a_new_node_is_connecting_until_its_first_check(self, h):
        view = h.registry.view("n1")
        assert (view.state, view.name, view.enabled) == (reg.STATE_CONNECTING, "nuc", True)
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "connecting to node")

    def test_a_node_that_answers_with_the_apps_version_is_ready(self, h):
        h.client().default_info = _info(gpus=[GPU_OK])
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_READY and view.reason == ""
        assert (view.version, view.roles, view.cpu_cores, view.legacy) == (APP_VERSION, (ROLE_WORKER,), 8, False)
        assert view.gpus == ({"type": "NVIDIA", "device": "cuda:0", "name": "RTX", "status": "ok"},)
        assert view.last_seen == 1_700_000_000.0
        assert h.registry.capacity("n1") == reg.NodeCapacity(True, True, "")
        # First contact records which node answers at this address.
        assert h.uids == [("n1", "uid-1")]

    def test_three_failed_attempts_make_it_offline(self, h):
        client = h.client()
        client.infos = [NodeUnreachableError("x")] * 3
        view = h.registry.check_now("n1")
        assert client.info_calls == 3
        assert view.state == reg.STATE_OFFLINE and "didn't answer" in view.reason
        assert "http://" not in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node offline")

    def test_one_failed_attempt_is_not_offline(self, h):
        client = h.client()
        client.infos = [NodeUnreachableError("x")]
        assert h.registry.check_now("n1").state == reg.STATE_READY
        assert client.info_calls == 2

    def test_offline_checks_back_off_to_five_minutes(self, h):
        client = h.client()
        client.default_info = NodeUnreachableError("down")
        delays = []
        for _ in range(5):
            h.registry.check_now("n1")
            delays.append(h.registry._nodes["n1"].next_check - h.clock.now)
        assert delays == [30.0, 60.0, 120.0, 300.0, 300.0]
        client.default_info = _info()
        h.registry.check_now("n1")
        assert h.registry._nodes["n1"].next_check - h.clock.now == reg.HEALTH_INTERVAL_S

    def test_a_refused_key_is_key_refused_after_one_attempt(self, h):
        client = h.client()
        client.default_info = NodeAuthError("no")
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_KEY_REFUSED and client.info_calls == 1
        assert h.registry.capacity("n1").reason == "key refused"

    def test_a_worker_on_another_version_needs_an_update(self, h):
        h.client().default_info = _info(version="9.9.8")
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_UPDATE_NEEDED
        assert "9.9.8" in view.reason and APP_VERSION in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node needs an update")

    def test_a_plex_database_only_node_needs_no_version_match(self, h):
        h.client().default_info = _info(version="1.0.0", roles=(ROLE_PLEX_DB,))
        assert h.registry.check_now("n1").state == reg.STATE_READY
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node doesn't run workers")

    def test_a_plex_database_role_older_than_the_agent_minimum_needs_an_update(self, h):
        h.client().default_info = _info(roles=(ROLE_PLEX_DB,), agent={"version": "0.1.0"})
        assert h.registry.check_now("n1").state == reg.STATE_UPDATE_NEEDED

    def test_a_different_node_at_the_address_is_offline(self, h):
        h.change(node_uid="uid-original")
        h.client().default_info = _info(uid="uid-other")
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_OFFLINE and "different node answered" in view.reason.lower()
        assert h.uids == []

    def test_a_turned_off_node_is_disabled_and_never_asked(self, h):
        h.change(enabled=False)
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_DISABLED and h.client().info_calls == 0
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node turned off")


class TestLegacyHelper:
    def test_an_old_helper_that_answers_its_ping_is_a_ready_plex_database_node(self, h):
        h.client().default_info = NodeProtocolError("404")
        view = h.registry.check_now("n1")
        assert (view.state, view.legacy, view.roles, view.version) == (
            reg.STATE_READY,
            True,
            (ROLE_PLEX_DB,),
            "1.2.0",
        )
        assert h.registry.capacity("n1").cpu is False

    @pytest.mark.parametrize(
        ("ping", "state"),
        [
            (("incompatible", "0.5.0"), reg.STATE_UPDATE_NEEDED),
            (("incompatible", "3.0.0"), reg.STATE_UPDATE_NEEDED),
            (("rejected", ""), reg.STATE_KEY_REFUSED),
            (("unreachable", ""), reg.STATE_OFFLINE),
        ],
        ids=["too-old", "newer-protocol", "key-refused", "not-a-helper"],
    )
    def test_what_the_ping_says_decides_the_state(self, h, ping, state):
        h.client().default_info = NodeProtocolError("404")
        h.legacy = ping
        assert h.registry.check_now("n1").state == state


class TestAccessProblem:
    def _ready(self, h):
        h.registry.check_now("n1")
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_three_different_files_in_ten_minutes_is_an_access_problem(self, h):
        self._ready(h)
        for path in ("/data/a.mkv", "/data/b.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
        assert h.registry.view("n1").state == reg.STATE_READY
        h.registry.report_not_visible("n1", "/data/c.mkv", "missing")
        view = h.registry.view("n1")
        assert view.state == reg.STATE_ACCESS_PROBLEM
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "fix media access first")

    def test_the_same_file_three_times_is_not(self, h):
        self._ready(h)
        for _ in range(3):
            h.registry.report_not_visible("n1", "/data/a.mkv", "missing")
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_files_spread_over_more_than_ten_minutes_are_not(self, h):
        self._ready(h)
        for path in ("/data/a.mkv", "/data/b.mkv", "/data/c.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
            h.clock.now += 301
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_a_passing_test_access_clears_it(self, h):
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)
        failing = {"libraries": [{"checked": 3, "ok": False}]}
        h.registry.record_access_result("n1", failing)
        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM
        h.registry.record_access_result("n1", {"libraries": [{"checked": 3, "ok": True}, {"checked": 0}]})
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry.details("n1").access_result["libraries"][0]["ok"] is True

    def test_check_now_clears_it_only_when_the_quick_access_check_passes(self, h):
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)
        h.quick = False
        assert h.registry.check_now("n1").state == reg.STATE_ACCESS_PROBLEM
        h.quick = True
        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_test_access_that_checked_nothing_does_not_pass(self):
        assert reg.access_passed({"libraries": [{"checked": 0, "ok": False}]}) is False
        assert reg.access_passed(None) is False


class TestGpuLost:
    def _ready_with_gpu(self, h):
        h.client().default_info = _info(gpus=[GPU_OK])
        h.registry.check_now("n1")

    def test_a_gpu_error_whose_rescan_fails_is_gpu_lost_and_cpu_keeps_running(self, h):
        self._ready_with_gpu(h)
        client = h.client()
        client.rescans = [{"gpus": [GPU_BAD]}]
        h.registry.report_gpu_error("n1", "cuda:0")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_GPU_LOST)
        capacity = h.registry.capacity("n1")
        assert (capacity.cpu, capacity.gpu) == (True, False) and "driver gone" in capacity.reason

    def test_a_missing_device_is_gpu_lost(self, h):
        self._ready_with_gpu(h)
        h.client().rescans = [{"worker": {"gpus": []}}]
        h.registry.report_gpu_error("n1", "cuda:0")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_GPU_LOST)
        assert "no longer finds it" in h.registry.view("n1").reason

    def test_a_gpu_error_whose_rescan_is_fine_changes_nothing(self, h):
        self._ready_with_gpu(h)
        h.registry.report_gpu_error("n1", "cuda:0")
        assert _wait_for(lambda: h.client().rescan_calls == 1)
        assert h.registry.view("n1").state == reg.STATE_READY

    def test_it_clears_when_the_gpu_is_back(self, h):
        self.test_a_gpu_error_whose_rescan_fails_is_gpu_lost_and_cpu_keeps_running(h)
        assert h.registry.rescan_gpus("n1").state == reg.STATE_READY

    def test_the_health_loop_rechecks_a_lost_gpu_every_five_minutes(self, h):
        self.test_a_gpu_error_whose_rescan_fails_is_gpu_lost_and_cpu_keeps_running(h)
        client = h.client()
        calls = client.rescan_calls
        h.registry._nodes["n1"].next_check = h.clock.now + 10_000  # keep the plain check out of the way
        h.registry._tick()
        assert client.rescan_calls == calls
        h.clock.now += reg.GPU_LOST_RECHECK_S
        h.registry._tick()
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)


class TestRevisionAndSettings:
    def test_revision_moves_on_state_changes_only(self, h):
        start = h.registry.revision
        h.registry.check_now("n1")
        after_ready = h.registry.revision
        assert after_ready > start
        h.registry.check_now("n1")
        assert h.registry.revision == after_ready
        h.client().default_info = NodeAuthError("no")
        h.registry.check_now("n1")
        assert h.registry.revision > after_ready

    def test_a_rename_bumps_the_revision_but_keeps_the_state(self, h):
        h.registry.check_now("n1")
        before = h.registry.revision
        h.change(name="renamed")
        assert h.registry.view("n1").name == "renamed"
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry.revision > before

    @pytest.mark.parametrize(
        "fields",
        [{"url": "http://moved:9494"}, {"key": "new"}, {"path_mappings": []}],
        ids=["url", "key", "mappings"],
    )
    def test_a_new_address_key_or_mappings_goes_back_to_connecting(self, h, fields):
        h.registry.check_now("n1")
        old_client = h.client()
        h.change(**fields)
        assert h.registry.view("n1").state == reg.STATE_CONNECTING
        new_client = h.client()
        assert (new_client is not old_client) == ("path_mappings" not in fields)
        assert h.registry._nodes["n1"].next_check == 0.0

    def test_a_removed_node_has_no_capacity_and_no_client(self, h):
        h.nodes.clear()
        h.revision += 1
        assert h.registry.view("n1") is None
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node removed")
        assert h.registry.mappings("n1") == []
        with pytest.raises(KeyError):
            h.registry.client("n1")

    def test_mappings_come_from_the_record(self, h):
        assert [m.to_json() for m in h.registry.mappings("n1")] == [{"app_prefix": "/data", "node_prefix": "/mnt"}]

    def test_details_keep_the_last_info_while_offline(self, h):
        h.registry.check_now("n1")
        h.client().default_info = NodeUnreachableError("x")
        h.registry.check_now("n1")
        details = h.registry.details("n1")
        assert details.view.state == reg.STATE_OFFLINE
        assert details.cpu["cores"] == 8 and details.platform["os"] == "Linux"


class TestLoop:
    def test_the_loop_checks_new_nodes_and_stops(self, h):
        h.registry.start()
        h.registry.start()  # idempotent
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        h.registry.stop()
        assert h.registry._thread is None

    def test_report_unavailable_checks_now_without_the_loop(self, h):
        h.registry.check_now("n1")
        client = h.client()
        client.default_info = NodeUnreachableError("gone")
        h.registry.report_unavailable("n1")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_OFFLINE)

    def test_report_unavailable_wakes_the_running_loop(self, h):
        h.registry.start()
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        h.client().default_info = NodeUnreachableError("gone")
        h.registry.report_unavailable("n1")
        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_OFFLINE)

    def test_unknown_nodes_are_ignored_by_every_report(self, h):
        h.registry.report_unavailable("nope")
        h.registry.report_not_visible("nope", "/x", "missing")
        h.registry.report_gpu_error("nope", "cuda:0")
        h.registry.record_access_result("nope", {})
        assert h.registry.check_now("nope") is None
        assert h.registry.rescan_gpus("nope") is None


def test_get_registry_is_a_singleton_and_set_registry_replaces_it():
    first = reg.get_registry()
    assert reg.get_registry() is first
    reg.set_registry(None)
    assert reg.get_registry() is not first


def test_the_app_factory_starts_the_registry_and_stops_it_at_exit(monkeypatch):
    import media_preview_generator.web.app as app_mod

    calls = []

    class Stub:
        def start(self):
            calls.append("start")

        def stop(self):
            calls.append("stop")

    # The autouse conftest fixture replaced the starter on this same monkeypatch; undo puts the real one back.
    monkeypatch.undo()
    stub = Stub()
    reg.set_registry(stub)
    registered = []
    monkeypatch.setattr(app_mod.atexit, "register", registered.append)
    app_mod._start_node_registry()
    assert calls == ["start"] and registered == [stub.stop]


class TestAccessResultSurvivesRestart:
    """The lab's app restart made every node's Setup Health ask for Test access again: the result lived in memory."""

    RESULT = {"libraries": [{"server": "Home", "library": "TV", "checked": 3, "ok": True, "files": []}]}

    @pytest.fixture
    def store(self, tmp_path):
        return AccessResultStore(lambda: str(tmp_path))

    def test_a_restarted_app_still_has_the_last_test_of_the_saved_mappings(self, store, tmp_path):
        before = Harness(access_store=store)
        before.registry.record_access_result("n1", self.RESULT)

        after = Harness(access_store=store)  # a new process: nothing in memory

        details = after.registry.details("n1")
        assert details.access_result == self.RESULT
        assert details.access_checked_at == 1_700_000_000.0
        saved = json.loads((tmp_path / "node_access_results.json").read_text())
        assert saved["n1"]["mappings"] == [{"app_prefix": "/data", "node_prefix": "/mnt"}]

    def test_changing_the_nodes_mappings_forgets_its_result(self, store):
        h = Harness(access_store=store)
        h.registry.record_access_result("n1", self.RESULT)

        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])

        assert h.registry.details("n1").access_result is None
        assert Harness(nodes=list(h.nodes), access_store=store).registry.details("n1").access_result is None

    def test_a_result_saved_for_other_mappings_is_never_handed_out(self, store):
        store.save("n1", [{"app_prefix": "/data", "node_prefix": "/old"}], self.RESULT, 1.0)
        assert Harness(access_store=store).registry.details("n1").access_result is None

    def test_removed_nodes_are_dropped_from_the_file_on_the_next_save(self, store, tmp_path):
        store.save("gone", [], self.RESULT, 1.0)
        Harness(access_store=store).registry.record_access_result("n1", self.RESULT)
        assert set(json.loads((tmp_path / "node_access_results.json").read_text())) == {"n1"}

    def test_an_unreadable_file_reads_as_untested(self, store, tmp_path):
        (tmp_path / "node_access_results.json").write_text("{not json")
        assert Harness(access_store=store).registry.details("n1").access_result is None
