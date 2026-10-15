"""NodeRegistry: the node state machine, capacity, reports from job code and the health loop."""

from __future__ import annotations

import json
import threading
import time

import pytest

from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.access_results import AccessResultStore
from media_preview_generator.nodes.client import (
    NodeAuthError,
    NodeProtocolError,
    NodeTimeoutError,
    NodeUnreachableError,
)
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
        self.libraries: list[str] = []
        self.clock = Clock()
        self.app_build = ""
        self.registry = reg.NodeRegistry(
            revision_provider=lambda: self.revision,
            nodes_provider=lambda: [dict(n) for n in self.nodes],
            client_factory=self._client,
            legacy_ping=lambda url, key: self.legacy,
            record_uid=lambda node_id, uid, url: self.uids.append((node_id, uid)),
            quick_access=lambda client, mappings: self.quick,
            library_names=lambda paths: list(self.libraries),
            app_version=lambda: APP_VERSION,
            app_build=lambda: self.app_build,
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


MAPPINGS = [{"app_prefix": "/data", "node_prefix": "/mnt"}]


def _record(**fields):
    return {
        "id": "n1",
        "node_uid": None,
        "name": "nuc",
        "url": "http://nuc:9494",
        "key": "k",
        "enabled": True,
        "path_mappings": list(MAPPINGS),
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

    def test_a_node_that_takes_connections_but_never_answers_says_so(self, h):
        client = h.client()
        client.infos = [NodeTimeoutError("x")] * 3
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_OFFLINE
        assert view.reason.startswith("nuc accepts connections but doesn't answer.")

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

    def test_a_worker_on_another_build_of_the_same_version_needs_an_update(self, h):
        # Pull request images and local builds all report one version: their commit tells them apart.
        h.app_build = "a" * 40
        info = _info()
        info["node"]["build"] = "b" * 40
        h.client().default_info = info

        view = h.registry.check_now("n1")

        assert view.state == reg.STATE_UPDATE_NEEDED
        assert "bbbbbbb" in view.reason and "aaaaaaa" in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node needs an update")

    @pytest.mark.parametrize(
        ("app_build", "node_build"),
        [
            ("a" * 40, "a" * 40),
            ("", "b" * 40),
            ("a" * 40, ""),
            ("abc1234" + "0" * 33, "abc1234"),
            ("abc1234", "ABC1234" + "0" * 33),
        ],
        ids=["same", "app-unknown", "node-unknown", "node-short", "app-short"],
    )
    def test_the_same_build_or_an_unknown_one_is_ready(self, h, app_build, node_build):
        h.app_build = app_build
        info = _info()
        info["node"]["build"] = node_build
        h.client().default_info = info

        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_short_commit_of_another_build_needs_an_update(self, h):
        h.app_build = "abc1234" + "0" * 33
        info = _info()
        info["node"]["build"] = "abc1235"
        h.client().default_info = info

        assert h.registry.check_now("n1").state == reg.STATE_UPDATE_NEEDED

    def test_a_plex_database_only_node_on_another_build_stays_ready(self, h):
        h.app_build = "a" * 40
        info = _info(roles=(ROLE_PLEX_DB,))
        info["node"]["build"] = "b" * 40
        h.client().default_info = info

        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_plex_database_only_node_needs_no_version_match(self, h):
        h.client().default_info = _info(version="1.0.0", roles=(ROLE_PLEX_DB,))
        assert h.registry.check_now("n1").state == reg.STATE_READY
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "node doesn't run workers")

    def test_a_plex_database_role_older_than_the_agent_minimum_needs_an_update(self, h):
        h.client().default_info = _info(roles=(ROLE_PLEX_DB,), agent={"version": "0.1.0"})
        assert h.registry.check_now("n1").state == reg.STATE_UPDATE_NEEDED

    def test_a_reinstalled_node_with_a_new_id_stays_ready_and_its_new_id_is_recorded(self, h):
        # The saved key at the saved address is what makes it this node: a recreated container's new id is no refusal.
        h.change(node_uid="uid-original")
        h.client().default_info = _info(uid="uid-reinstalled")
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_READY and view.reason == ""
        assert h.registry.capacity("n1") == reg.NodeCapacity(True, True, "")
        assert h.uids == [("n1", "uid-reinstalled")]

    def test_the_id_on_record_is_not_written_again(self, h):
        h.change(node_uid="uid-1")
        h.client().default_info = _info(uid="uid-1")
        h.registry.check_now("n1")
        assert h.uids == []

    def test_a_worker_node_without_ffmpeg_is_not_ready_and_gets_no_work(self, h):
        info = _info()
        info["worker"]["ffmpeg"] = {"path": None, "version": None}
        h.client().default_info = info
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_NO_FFMPEG and "can't find FFmpeg" in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "FFmpeg missing on node")
        assert reg.STATE_NO_FFMPEG in reg.ALERT_STATES

    def test_a_worker_node_whose_ffmpeg_doesnt_run_is_not_ready_and_gets_no_work(self, h):
        info = _info()
        info["worker"]["ffmpeg"] = {"path": "/usr/bin/ffmpeg", "version": None}
        h.client().default_info = info
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_NO_FFMPEG and "/usr/bin/ffmpeg, but it doesn't run" in view.reason
        assert h.registry.capacity("n1") == reg.NodeCapacity(False, False, "FFmpeg missing on node")

    def test_a_worker_node_with_ffmpeg_is_ready(self, h):
        info = _info()
        info["worker"]["ffmpeg"] = {"path": "/usr/bin/ffmpeg", "version": "7.1"}
        h.client().default_info = info
        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_a_node_on_another_node_protocol_needs_an_update_naming_both_versions(self, h):
        node = {"id": "uid-1", "name": "nuc", "version": "9.10.0", "protocols": [2], "roles": [ROLE_WORKER]}
        legacy_asked = []
        h.registry._legacy_ping = lambda url, key: legacy_asked.append(url) or ("connected", "1.2.0")
        h.client().default_info = NodeProtocolError("another protocol", node=node)
        view = h.registry.check_now("n1")
        assert view.state == reg.STATE_UPDATE_NEEDED
        assert "9.10.0" in view.reason and APP_VERSION in view.reason
        assert (view.version, view.legacy) == ("9.10.0", False)
        assert legacy_asked == []

    def test_an_answer_without_a_node_block_is_tried_as_an_older_plex_helper(self, h):
        h.client().default_info = NodeProtocolError("doesn't know /v1/info")
        view = h.registry.check_now("n1")
        assert (view.state, view.legacy) == (reg.STATE_READY, True)

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

    def test_it_names_the_libraries_of_the_files_the_node_couldnt_see(self, h):
        self._ready(h)
        h.libraries = ["Open Films"]
        for path in ("/data/a.mkv", "/data/b.mkv", "/data/c.mkv"):
            h.registry.report_not_visible("n1", path, "missing")
        view = h.registry.view("n1")
        assert view.state == reg.STATE_ACCESS_PROBLEM
        assert view.reason.startswith("nuc couldn't see 3 different files in Open Films in 10 minutes")

    def test_the_libraries_are_named_from_the_deepest_library_folder_above_each_file(self, monkeypatch):
        from media_preview_generator.web import settings_manager

        servers = [
            {
                "id": "p1",
                "type": "plex",
                "name": "Home",
                "enabled": True,
                "url": "http://p:32400",
                "libraries": [
                    {"id": "1", "name": "Media", "remote_paths": ["/media"]},
                    {"id": "2", "name": "Open Films", "remote_paths": ["/media/openfilms/Movies"]},
                    {"id": "3", "name": "TV", "remote_paths": ["/media/tv"]},
                ],
            }
        ]

        class Settings:
            def get(self, key, default=None):
                return servers if key == "media_servers" else default

        monkeypatch.setattr(settings_manager, "get_settings_manager", lambda: Settings())
        paths = ["/media/openfilms/Movies/a.mkv", "/media/openfilms/Movies/b.mkv", "/media/other/c.mkv", "/elsewhere"]
        assert reg._default_library_names(paths) == ["Open Films", "Media"]

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
        h.registry.record_access_result("n1", failing, tested_mappings=MAPPINGS)
        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM
        h.registry.record_access_result(
            "n1", {"libraries": [{"checked": 3, "ok": True}, {"checked": 0}]}, tested_mappings=MAPPINGS
        )
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry.details("n1").access_result["libraries"][0]["ok"] is True

    def test_a_pass_of_mappings_replaced_since_the_test_began_changes_nothing(self, h):
        tested = list(h.nodes[0]["path_mappings"])
        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])
        # The new mappings fail on three files while the test of the old ones is still running.
        self.test_three_different_files_in_ten_minutes_is_an_access_problem(h)

        h.registry.record_access_result("n1", {"libraries": [{"checked": 3, "ok": True}]}, tested_mappings=tested)

        assert h.registry.view("n1").state == reg.STATE_ACCESS_PROBLEM
        assert h.registry.details("n1").access_result is None

    NEW_ROWS = [{"app_prefix": "/data", "node_prefix": "/volume1"}]
    PASS = {"libraries": [{"checked": 3, "ok": True}]}

    def test_a_test_of_unsaved_rows_is_adopted_when_those_rows_are_saved(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)
        assert h.registry.details("n1").access_result is None

        h.change(path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result == self.PASS

    def test_a_held_test_is_dropped_when_the_address_changes_with_the_rows(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)

        h.change(url="http://other:9494", path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result is None

    def test_a_held_test_is_dropped_when_the_key_changes(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)
        h.change(key="another-key")

        h.change(path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result is None

    def test_a_held_test_expires_after_ten_minutes(self, h):
        h.registry.record_access_result("n1", self.PASS, tested_mappings=self.NEW_ROWS)
        h.clock.now += reg.PENDING_ACCESS_TTL_S + 1

        h.change(path_mappings=self.NEW_ROWS)

        assert h.registry.details("n1").access_result is None

    def test_a_test_of_an_address_the_node_no_longer_has_is_dropped(self, h):
        h.change(url="http://other:9494")

        h.registry.record_access_result("n1", self.PASS, tested_mappings=MAPPINGS, tested_url="http://nuc:9494")

        assert h.registry.details("n1").access_result is None

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

    def test_a_node_that_couldnt_detect_its_gpus_yet_pauses_gpu_members_until_it_can(self, h):
        from media_preview_generator.jobs.node_members import NodeHardware

        failing = _info()
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing

        view = h.registry.check_now("n1")

        assert view.state == reg.STATE_GPU_LOST and "couldn't check its GPUs" in view.reason
        capacity = h.registry.capacity("n1")
        assert (capacity.cpu, capacity.gpu) == (True, False)
        hardware = NodeHardware(1, {"n1": view}, {"n1": capacity})
        assert hardware.member_can_come_back({"node": "n1", "resource": "gpu", "device": "cuda:0"})

        h.client().default_info = _info(gpus=[GPU_OK])
        assert h.registry.check_now("n1").state == reg.STATE_READY

    def test_the_health_loop_rescans_a_node_that_couldnt_detect_its_gpus(self, h):
        failing = _info()
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing
        h.registry.check_now("n1")
        h.registry._nodes["n1"].next_check = h.clock.now + 10_000
        h.clock.now += reg.GPU_LOST_RECHECK_S

        h.registry._tick()

        assert _wait_for(lambda: h.registry.view("n1").state == reg.STATE_READY)
        assert h.client().rescan_calls == 1

    def test_a_rescan_that_still_cant_detect_keeps_the_check_pending(self, h):
        failing = _info()
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing
        h.registry.check_now("n1")
        h.client().rescans = [{"worker": {"gpus": [], "gpu_error": "RuntimeError: GPU busy"}}]

        assert h.registry.rescan_gpus("n1").state == reg.STATE_GPU_LOST

    def test_a_plex_database_only_node_with_a_detection_error_stays_ready(self, h):
        failing = _info(roles=(ROLE_PLEX_DB,))
        failing["worker"]["gpu_error"] = "RuntimeError: GPU busy"
        h.client().default_info = failing

        assert h.registry.check_now("n1").state == reg.STATE_READY


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

    def test_only_a_check_begun_after_a_drop_vouches_for_the_node(self, h):
        h.registry.check_now("n1")
        assert h.registry.checked_ready_since_unavailable("n1") is True
        client = h.client()
        started, release = threading.Event(), threading.Event()

        def slow_info():
            started.set()
            release.wait(5)
            return _info()

        client.info = slow_info
        checking = threading.Thread(target=h.registry.check_now, args=("n1",))
        checking.start()
        assert started.wait(5)
        h.registry._run_check = lambda *args, **kwargs: None  # keep the report's own check out of it
        h.registry.report_unavailable("n1")
        release.set()
        checking.join(5)

        # Ready, but that check began before the node dropped its file; the next one is due at once.
        assert h.registry.view("n1").state == reg.STATE_READY
        assert h.registry.checked_ready_since_unavailable("n1") is False
        assert h.registry._nodes["n1"].next_check == 0.0
        del h.registry._run_check
        client.info = lambda: _info()
        h.registry.check_now("n1")
        assert h.registry.checked_ready_since_unavailable("n1") is True

    def test_a_check_begun_after_a_drop_that_finds_the_node_down_vouches_for_nothing(self, h):
        h.registry.check_now("n1")
        h.registry._run_check = lambda *args, **kwargs: None
        h.registry.report_unavailable("n1")
        del h.registry._run_check
        h.client().infos = [NodeUnreachableError("x")] * 3
        h.registry.check_now("n1")
        assert h.registry.checked_ready_since_unavailable("n1") is False

    def test_unknown_nodes_are_ignored_by_every_report(self, h):
        h.registry.report_unavailable("nope")
        h.registry.report_not_visible("nope", "/x", "missing")
        h.registry.report_gpu_error("nope", "cuda:0")
        h.registry.record_access_result("nope", {}, tested_mappings=MAPPINGS)
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


class _LockProbingStore(AccessResultStore):
    """Records, for each file read or write, whether another thread could take the registry's lock meanwhile."""

    def __init__(self, folder: str) -> None:
        super().__init__(lambda: folder)
        self.registry: reg.NodeRegistry | None = None
        self.calls: list[tuple[str, bool]] = []

    def _lock_free(self) -> bool:
        free: list[bool] = []

        def probe() -> None:
            got = self.registry._lock.acquire(timeout=1.0)
            if got:
                self.registry._lock.release()
            free.append(got)

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return free[0]

    def load(self, node_id, mappings):
        self.calls.append(("load", self._lock_free()))
        return super().load(node_id, mappings)

    def forget(self, node_id, **kwargs):
        self.calls.append(("forget", self._lock_free()))
        return super().forget(node_id, **kwargs)


class TestAccessStoreIoOutsideTheLock:
    """The store reads and writes a file; under the registry's lock every view() (each worker's capacity read, each
    page load) would wait on that disk."""

    RESULT = {"libraries": [{"checked": 3, "ok": True}]}

    def test_a_new_nodes_saved_result_is_read_outside_the_lock_and_still_handed_out(self, tmp_path):
        AccessResultStore(lambda: str(tmp_path)).save("n1", MAPPINGS, self.RESULT, 5.0)
        store = _LockProbingStore(str(tmp_path))
        h = Harness(access_store=store)
        store.registry = h.registry

        assert h.registry.details("n1").access_result == self.RESULT
        assert store.calls == [("load", True)]

    def test_changed_mappings_forget_the_result_outside_the_lock(self, tmp_path):
        store = _LockProbingStore(str(tmp_path))
        h = Harness(access_store=store)
        store.registry = h.registry
        h.registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)

        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])
        h.registry.views()

        assert store.calls[-1] == ("forget", True)
        assert h.registry.details("n1").access_result is None
        assert json.loads((tmp_path / "node_access_results.json").read_text()) == {}

    def test_a_result_saved_for_the_new_mappings_before_the_old_one_is_forgotten_is_kept(self, tmp_path):
        store = AccessResultStore(lambda: str(tmp_path))
        new = [{"app_prefix": "/data", "node_prefix": "/volume1"}]
        store.save("n1", new, self.RESULT, 5.0)

        store.forget("n1", keep_mappings=new)

        assert store.load("n1", new) == (self.RESULT, 5.0)


class TestAccessResultSurvivesRestart:
    """The lab's app restart made every node's Setup Health ask for Test access again: the result lived in memory."""

    RESULT = {"libraries": [{"server": "Home", "library": "TV", "checked": 3, "ok": True, "files": []}]}

    @pytest.fixture
    def store(self, tmp_path):
        return AccessResultStore(lambda: str(tmp_path))

    def test_a_restarted_app_still_has_the_last_test_of_the_saved_mappings(self, store, tmp_path):
        before = Harness(access_store=store)
        before.registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)

        after = Harness(access_store=store)  # a new process: nothing in memory

        details = after.registry.details("n1")
        assert details.access_result == self.RESULT
        assert details.access_checked_at == 1_700_000_000.0
        saved = json.loads((tmp_path / "node_access_results.json").read_text())
        assert saved["n1"]["mappings"] == [{"app_prefix": "/data", "node_prefix": "/mnt"}]

    def test_changing_the_nodes_mappings_forgets_its_result(self, store):
        h = Harness(access_store=store)
        h.registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)

        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])

        assert h.registry.details("n1").access_result is None
        assert Harness(nodes=list(h.nodes), access_store=store).registry.details("n1").access_result is None

    def test_a_result_saved_for_other_mappings_is_never_handed_out(self, store):
        store.save("n1", [{"app_prefix": "/data", "node_prefix": "/old"}], self.RESULT, 1.0)
        assert Harness(access_store=store).registry.details("n1").access_result is None

    def test_a_result_for_mappings_replaced_during_the_test_is_not_saved(self, store, tmp_path):
        h = Harness(access_store=store)
        tested = list(h.nodes[0]["path_mappings"])
        h.change(path_mappings=[{"app_prefix": "/data", "node_prefix": "/volume1"}])

        h.registry.record_access_result("n1", self.RESULT, tested_mappings=tested)

        assert not (tmp_path / "node_access_results.json").exists()
        assert Harness(nodes=list(h.nodes), access_store=store).registry.details("n1").access_result is None

    def test_removed_nodes_are_dropped_from_the_file_on_the_next_save(self, store, tmp_path):
        store.save("gone", [], self.RESULT, 1.0)
        Harness(access_store=store).registry.record_access_result("n1", self.RESULT, tested_mappings=MAPPINGS)
        assert set(json.loads((tmp_path / "node_access_results.json").read_text())) == {"n1"}

    def test_an_unreadable_file_reads_as_untested(self, store, tmp_path):
        (tmp_path / "node_access_results.json").write_text("{not json")
        assert Harness(access_store=store).registry.details("n1").access_result is None


class TestNewPlexDatabaseNodeId:
    """A Plex database node that reports a new id is taken silently only when its Plex is the one it serves."""

    def _harness(self, served):
        harness = Harness(nodes=[_record(node_uid="uid-1")])
        harness.registry._served_plex_ids = lambda node_id: list(served)
        return harness

    def _answer(self, h, uid, machine_identifier="plex-a", roles=(ROLE_PLEX_DB,)):
        info = _info(uid=uid, roles=roles)
        info["plex_db"] = {"machine_identifier": machine_identifier}
        h.client().default_info = info
        h.registry.check_now("n1")

    @pytest.mark.parametrize(
        ("served", "machine_identifier", "recorded"),
        [
            (["plex-a"], "plex-a", True),
            ([], "", True),
            (["plex-a"], "", False),
            ([""], "plex-a", False),
            (["plex-a", "plex-b"], "plex-a", False),
        ],
        ids=["same-plex", "no-server-uses-it", "node-doesnt-say", "server-doesnt-say", "two-plexes"],
    )
    def test_when_its_new_id_is_recorded(self, served, machine_identifier, recorded):
        h = self._harness(served)
        self._answer(h, "uid-2", machine_identifier)
        assert (h.uids == [("n1", "uid-2")]) is recorded
        assert (h.registry.plex_db_refusal("n1") == "") is recorded
        h.registry.stop()

    def test_a_worker_node_with_a_new_id_is_recorded_whatever_its_plex(self):
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "", roles=(ROLE_WORKER,))
        assert h.uids == [("n1", "uid-2")] and h.registry.plex_db_refusal("n1") == ""
        h.registry.stop()

    def test_the_refusal_lifts_when_the_saved_id_is_cleared(self):
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "")
        assert "Type the node's key again" in h.registry.plex_db_refusal("n1")

        h.change(node_uid=None)

        assert h.registry.plex_db_refusal("n1") == ""
        h.registry.stop()

    def test_the_old_id_answering_again_lifts_the_refusal(self):
        h = self._harness(["plex-a"])
        self._answer(h, "uid-2", "")
        self._answer(h, "uid-1", "")
        assert h.registry.plex_db_refusal("n1") == ""
        h.registry.stop()


class TestRecordedNodeId:
    """What ``_default_record_uid`` writes to settings.json."""

    @pytest.fixture
    def sm(self, tmp_path):
        from media_preview_generator.web import settings_manager as sm_mod
        from media_preview_generator.web.settings_manager import SettingsManager

        manager = SettingsManager(config_dir=str(tmp_path))
        sm_mod._settings_manager = manager
        manager.update_nodes([{**_record(), "added_at": "2026-10-15T00:00:00+00:00"}])
        yield manager
        sm_mod.reset_settings_manager()

    def test_a_new_id_replaces_the_one_on_record(self, sm):
        reg._default_record_uid("n1", "uid-first", "http://nuc:9494")
        reg._default_record_uid("n1", "uid-reinstalled", "http://nuc:9494")
        assert sm.nodes[0]["node_uid"] == "uid-reinstalled"

    def test_an_id_learnt_at_an_address_the_node_no_longer_has_is_not_recorded(self, sm):
        reg._default_record_uid("n1", "uid-old-machine", "http://old-nuc:9494")
        assert sm.nodes[0]["node_uid"] is None

    def test_a_check_of_the_old_address_finishing_after_an_address_change_records_nothing(self, sm):
        """The PUT saves the new address before the registry syncs, so the check's ``live.client`` guard can't see
        the change: the address the id came from is what keeps the old machine's id off the new address."""
        release, asked = threading.Event(), threading.Event()

        class SlowOldMachine(FakeClient):
            def info(self):
                asked.set()
                release.wait(5)
                return _info(uid="old-machine")

        registry = reg.NodeRegistry(client_factory=SlowOldMachine, app_version=lambda: APP_VERSION, attempt_gap_s=0.0)
        registry.views()
        check = threading.Thread(target=registry._run_check, args=("n1",))
        check.start()
        assert asked.wait(5)
        with sm.locked():
            sm.update_nodes([{**sm.nodes[0], "url": "http://new-nuc:9494", "node_uid": None}])
        release.set()
        check.join(5)

        assert (sm.nodes[0]["url"], sm.nodes[0]["node_uid"]) == ("http://new-nuc:9494", None)


class TestAgainstARealNode:
    """The registry's check against the node's own server."""

    KEY = "registry-real-node-key-0123456789"

    @pytest.fixture
    def node_url(self, tmp_path, monkeypatch):
        from werkzeug.serving import make_server

        from media_preview_generator.nodes.server import app as node_app
        from media_preview_generator.nodes.server import info

        for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_ROLES"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-tmp"))
        monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"})
        app = node_app.create_node_app(config_dir=str(tmp_path / "config"), roles=["worker"], key=self.KEY, ops={})
        app.extensions[node_app.EXTENSION].worker_info._detect = lambda: []
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}"
        server.shutdown()
        thread.join(5)
        app.extensions[node_app.EXTENSION].close()

    def test_a_worker_node_on_another_node_protocol_needs_an_update(self, node_url):
        import requests

        from media_preview_generator import __version__
        from media_preview_generator.nodes import protocol
        from media_preview_generator.nodes.client import NodeAddress, NodeClient

        class NewerAppClient(NodeClient):
            """The app after a protocol bump (it asks for protocol 2); the node is still on protocol 1."""

            def _headers(self):
                return {"Authorization": f"Bearer {TestAgainstARealNode.KEY}", protocol.PROTOCOL_HEADER: "2"}

        record = _record(url=node_url, key=self.KEY, path_mappings=[])
        session = requests.Session()
        session.trust_env = False
        registry = reg.NodeRegistry(
            revision_provider=lambda: 1,
            nodes_provider=lambda: [record],
            record_uid=lambda *_: None,
            legacy_ping=lambda url, key: pytest.fail("a node is never asked the old helper's ping"),
            app_version=lambda: "0.0.1",
            attempt_gap_s=0.0,
        )
        outcome = registry._probe(record, NewerAppClient(NodeAddress(node_url, self.KEY), session=session))
        assert outcome.state == reg.STATE_UPDATE_NEEDED, outcome.reason
        assert __version__ in outcome.reason and "0.0.1" in outcome.reason


def test_a_saved_address_urllib3_refuses_reads_offline_not_connecting_forever():
    # Saved before the address rules refused it: requests prepares it, urllib3 refuses it before connecting.
    record = {"id": "n1", "name": "nas", "url": "http://nas..lan:9494", "key": "k", "enabled": True}
    registry = reg.NodeRegistry(
        nodes_provider=lambda: [record], revision_provider=lambda: 1, record_uid=lambda *a: None, attempt_gap_s=0.0
    )
    view = registry.check_now("n1")
    assert view is not None and view.state == reg.STATE_OFFLINE
