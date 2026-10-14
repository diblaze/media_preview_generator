"""The Nodes page REST API (spec §10.4), through the Flask test client."""

from __future__ import annotations

import time

import pytest

from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.client import (
    NodeAuthError,
    NodeProtocolError,
    NodeRequestError,
    NodeUnreachableError,
)
from media_preview_generator.web.routes import api_nodes

APP_VERSION = "9.9.9"
NODE_ROW_KEYS = {
    "id",
    "name",
    "url",
    "enabled",
    "state",
    "reason",
    "version",
    "roles",
    "legacy",
    "platform",
    "gpus",
    "cpu",
    "busy",
    "last_seen",
    "path_mappings",
    "used_by",
}
GPU = {"type": "NVIDIA", "device": "cuda:0", "name": "RTX", "status": "ok", "error": None}
GPU_ON_CPU_TEXT = {**GPU, "credit_text": {"on_gpu": False, "reason": "Vulkan reports no hardware GPU"}}


def _info(uid="uid-1", version=APP_VERSION):
    return {
        "ok": True,
        "node": {"id": uid, "name": "nuc", "version": version, "protocols": [1], "roles": ["worker"]},
        "platform": {"os": "Linux", "arch": "x86_64", "python": "3.12"},
        "worker": {
            "gpus": [GPU],
            "cpu": {"cores": 8, "load1": 0.25, "ram_total": 16, "ram_available": 9},
            "temp": {"path": "/tmp/node", "free_bytes": 41_000_000_000},
            "busy": 2,
            "ffmpeg": {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7"},
        },
    }


class FakeNode:
    """One fake node per address; ``info`` may be an exception to raise."""

    def __init__(self):
        self.info_answer: object = _info()
        self.visible = True
        self.log_answer: object = {
            "lines": [{"time": "2026-10-15T09:00:00+00:00", "level": "INFO", "message": "one"}, "two"]
        }
        self.access_requests: list = []

    def info(self):
        if isinstance(self.info_answer, Exception):
            raise self.info_answer
        return self.info_answer

    def rescan_gpus(self):
        return {"gpus": [GPU]}

    def access_check(self, mappings, files):
        self.access_requests.append((mappings, files))
        return {
            "results": [
                {"path": f["path"], "ok": self.visible, "node_path": "/n" + f["path"], "kind": "", "message": ""}
                for f in files
            ],
            "nearby": {},
        }

    def log(self, lines=200):
        if isinstance(self.log_answer, Exception):
            raise self.log_answer
        return self.log_answer


@pytest.fixture
def nodes(client, monkeypatch):
    client.application.config["WTF_CSRF_ENABLED"] = False
    fakes: dict[str, FakeNode] = {}

    def fake(url: str) -> FakeNode:
        return fakes.setdefault(url, FakeNode())

    monkeypatch.setattr(api_nodes, "_make_client", lambda url, key: fake(url))
    monkeypatch.setattr(api_nodes, "_legacy_ping", lambda url, key: ("connected", "1.2.0"))
    monkeypatch.setattr(api_nodes, "_app_version", lambda: APP_VERSION)
    reg.set_registry(
        reg.NodeRegistry(
            client_factory=lambda record: fake(record["url"]),
            legacy_ping=lambda url, key: ("connected", "1.2.0"),
            quick_access=lambda client, mappings: True,
            app_version=lambda: APP_VERSION,
            attempt_gap_s=0.0,
        )
    )
    return fake


def _sm():
    from media_preview_generator.web.settings_manager import get_settings_manager

    return get_settings_manager()


def _add(client, **fields):
    body = {"name": "nuc", "url": "http://nuc:9494", "key": "node-key", "path_mappings": [], **fields}
    return client.post("/api/nodes", json=body)


class TestListAndProbe:
    def test_an_install_without_nodes(self, client, nodes):
        body = client.get("/api/nodes").get_json()
        assert body == {"nodes": [], "app_version": APP_VERSION}

    def test_probe_reports_what_answers_without_saving(self, client, nodes):
        resp = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "node-key"})
        body = resp.get_json()
        assert resp.status_code == 200 and body["ok"] is True
        assert body["info"] == {
            "node": {"id": "uid-1", "name": "nuc", "version": APP_VERSION, "roles": ["worker"]},
            "legacy": False,
            "platform": {"os": "Linux", "arch": "x86_64", "python": "3.12"},
            "worker": {
                "gpus": [GPU],
                "cpu": {"cores": 8, "load1": 0.25, "ram_total": 16, "ram_available": 9},
                "temp": {"path": "/tmp/node", "free_bytes": 41_000_000_000},
                "notes": [],
            },
            "plex_db": None,
            "version_match": True,
            "already_added": None,
            "suggested_path_mappings": [],
        }
        assert _sm().nodes == []

    def test_probe_turns_credit_text_on_the_cpu_into_a_note(self, client, nodes):
        answer = _info()
        answer["worker"]["gpus"] = [GPU_ON_CPU_TEXT]
        nodes("http://nuc:9494").info_answer = answer
        info = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert info["worker"]["notes"] == [
            {"title": "Credits text", "chip": "CPU on RTX", "message": "Vulkan reports no hardware GPU"}
        ]

    def test_probe_of_a_plex_database_node_passes_its_facts_through(self, client, nodes):
        answer = _info()
        answer["node"]["roles"] = ["plex_db"]
        del answer["worker"]
        answer["plex_db"] = {"config_dir": "/plex", "db_present": True, "machine_identifier": "m1"}
        nodes("http://nuc:9494").info_answer = answer
        info = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert (info["worker"], info["plex_db"], info["version_match"]) == (None, answer["plex_db"], None)
        assert info["suggested_path_mappings"] == []

    def test_probe_suggests_one_row_per_library_folder(self, client, nodes, tmp_path):
        movies = TestTestAccess()._library(tmp_path)
        info = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert info["suggested_path_mappings"] == [{"app_prefix": str(movies), "node_prefix": str(movies)}]

    def test_probe_says_when_the_version_differs(self, client, nodes):
        nodes("http://nuc:9494").info_answer = _info(version="1.0.0")
        info = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert info["version_match"] is False

    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (NodeAuthError("no"), "auth"),
            (NodeUnreachableError("down"), "unreachable"),
            (NodeRequestError("bad request"), "request"),
        ],
        ids=["auth", "unreachable", "request"],
    )
    def test_probe_errors(self, client, nodes, error, kind):
        nodes("http://nuc:9494").info_answer = error
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["ok"] is False and body["error"]["kind"] == kind and body["error"]["message"]

    def test_probe_of_an_old_helper(self, client, nodes):
        nodes("http://nuc:9494").info_answer = NodeProtocolError("404")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["ok"] is True
        info = body["info"]
        assert (info["legacy"], info["node"], info["worker"]) == (
            True,
            {"id": None, "name": "", "version": "1.2.0", "roles": ["plex_db"]},
            None,
        )
        assert (info["version_match"], info["already_added"]) == (None, None)

    def test_probe_refuses_a_bad_address(self, client, nodes):
        resp = client.post("/api/nodes/probe", json={"url": "ftp://x", "key": "k"})
        assert resp.status_code == 400 and "address" in resp.get_json()["error"]

    def test_probe_names_the_node_already_added(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"})
        assert resp.get_json()["info"]["already_added"] == {"id": node_id, "name": "nuc"}

    def test_probe_with_the_mask_uses_the_stored_key_and_isnt_its_own_twin(self, client, nodes, monkeypatch):
        node_id = _add(client).get_json()["id"]
        keys = []
        monkeypatch.setattr(api_nodes, "_make_client", lambda url, key: keys.append(key) or nodes(url))
        resp = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "****", "id": node_id})
        assert resp.get_json()["info"]["already_added"] is None and keys == ["node-key"]

    def test_probe_of_a_saved_node_says_when_a_different_node_answers(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        nodes("http://nuc:9494").info_answer = _info(uid="uid-other")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "****", "id": node_id}).get_json()
        assert body["ok"] is False and body["error"]["kind"] == "different_node" and "nuc" in body["error"]["message"]


class TestCreate:
    def test_adding_a_node_saves_it_and_answers_with_its_row(self, client, nodes):
        resp = _add(client, path_mappings=[{"app_prefix": "/data", "node_prefix": "/mnt"}])
        assert resp.status_code == 201, resp.get_json()
        row = resp.get_json()
        assert set(row) == NODE_ROW_KEYS
        assert "node-key" not in resp.get_data(as_text=True)
        assert (row["name"], row["url"], row["enabled"]) == ("nuc", "http://nuc:9494", True)
        assert row["path_mappings"] == [{"app_prefix": "/data", "node_prefix": "/mnt"}]
        assert row["used_by"] == {"groups": [], "servers": []}
        stored = _sm().nodes[0]
        assert (stored["id"], stored["node_uid"], stored["key"]) == (row["id"], "uid-1", "node-key")

    def test_the_same_node_twice_is_refused(self, client, nodes):
        first = _add(client).get_json()
        resp = _add(client, name="again")
        assert resp.status_code == 409
        assert (resp.get_json()["node_id"], resp.get_json()["name"]) == (first["id"], "nuc")
        assert len(_sm().nodes) == 1

    def test_an_unreachable_node_is_saved_and_records_its_id_later(self, client, nodes):
        nodes("http://nuc:9494").info_answer = NodeUnreachableError("down")
        resp = _add(client)
        assert resp.status_code == 201
        assert _sm().nodes[0]["node_uid"] is None

    @pytest.mark.parametrize(
        "fields",
        [{"name": ""}, {"url": "nope"}, {"key": ""}, {"path_mappings": [{"app_prefix": "x", "node_prefix": "y"}]}],
        ids=["name", "url", "key", "mappings"],
    )
    def test_invalid_nodes_are_refused(self, client, nodes, fields):
        resp = _add(client, **fields)
        assert resp.status_code == 400 and resp.get_json()["error"]
        assert _sm().nodes == []

    def test_a_duplicate_name_is_refused(self, client, nodes):
        _add(client)
        nodes("http://other:9494").info_answer = _info(uid="uid-2")
        resp = _add(client, url="http://other:9494", name="NUC")
        assert resp.status_code == 400 and "already called" in resp.get_json()["error"]


class TestRowState:
    def test_check_now_answers_with_the_live_state_and_last_info(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.post(f"/api/nodes/{node_id}/check")
        row = resp.get_json()
        assert resp.status_code == 200 and row["state"] == "ready"
        assert (row["version"], row["roles"], row["legacy"], row["busy"]) == (APP_VERSION, ["worker"], False, 2)
        assert row["cpu"] == {"cores": 8, "load1": 0.25, "ram_total": 16, "ram_available": 9}
        assert row["platform"] == {"os": "Linux", "arch": "x86_64", "python": "3.12"}
        assert row["gpus"] == [{"type": "NVIDIA", "device": "cuda:0", "name": "RTX", "status": "ok", "error": ""}]
        assert row["last_seen"] is not None
        listed = client.get("/api/nodes").get_json()["nodes"]
        assert [n["state"] for n in listed] == ["ready"]

    def test_rescan_gpus_answers_with_the_row(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.post(f"/api/nodes/{node_id}/rescan-gpus")
        assert resp.status_code == 200 and resp.get_json()["gpus"][0]["device"] == "cuda:0"

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/check"),
            ("post", "/rescan-gpus"),
            ("post", "/test-access"),
            ("get", "/health"),
            ("get", "/log"),
            ("put", ""),
            ("delete", ""),
        ],
    )
    def test_an_unknown_node_is_404(self, client, nodes, method, path):
        resp = getattr(client, method)(f"/api/nodes/nope{path}", json={})
        assert resp.status_code == 404


class TestUpdate:
    def test_a_rename_with_the_mask_keeps_the_key(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.put(f"/api/nodes/{node_id}", json={"name": "renamed", "key": "****", "id": "evil"})
        assert resp.status_code == 200 and resp.get_json()["name"] == "renamed"
        stored = _sm().nodes[0]
        assert (stored["id"], stored["key"], stored["node_uid"]) == (node_id, "node-key", "uid-1")

    def test_a_new_address_goes_back_to_connecting(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        client.post(f"/api/nodes/{node_id}/check")
        nodes("http://moved:9494").info_answer = NodeUnreachableError("slow")
        row = client.put(f"/api/nodes/{node_id}", json={"url": "http://moved:9494"}).get_json()
        assert row["url"] == "http://moved:9494"
        assert row["state"] in ("connecting", "offline")

    def test_turning_it_off(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        row = client.put(f"/api/nodes/{node_id}", json={"enabled": False}).get_json()
        assert (row["enabled"], row["state"]) == (False, "disabled")

    def test_an_invalid_change_is_refused_and_nothing_is_saved(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.put(f"/api/nodes/{node_id}", json={"url": "nope"})
        assert resp.status_code == 400
        assert _sm().nodes[0]["url"] == "http://nuc:9494"


class TestDelete:
    def test_a_node_nothing_uses_is_removed(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.delete(f"/api/nodes/{node_id}")
        assert resp.status_code == 200 and _sm().nodes == []

    def test_a_node_used_by_a_group_member_or_a_plex_server_is_kept(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        _sm().set(
            "worker_groups",
            [{"id": "g1", "name": "Night", "members": [{"id": "m1", "node": node_id}]}],
        )
        _sm().set(
            "media_servers",
            [
                {
                    "id": "p1",
                    "type": "plex",
                    "name": "Home",
                    "markers": {"plex": {"agent": {"enabled": False, "node_id": node_id}}},
                }
            ],
        )
        resp = client.delete(f"/api/nodes/{node_id}")
        body = resp.get_json()
        assert resp.status_code == 409
        assert body["used_by"] == [
            {"kind": "group", "id": "g1", "name": "Night"},
            {"kind": "server", "id": "p1", "name": "Home"},
        ]
        assert len(_sm().nodes) == 1
        row = client.get("/api/nodes").get_json()["nodes"][0]
        assert row["used_by"] == {"groups": [{"id": "g1", "name": "Night"}], "servers": [{"id": "p1", "name": "Home"}]}


class TestTestAccess:
    def _library(self, tmp_path):
        movies = tmp_path / "media" / "Movies"
        movies.mkdir(parents=True)
        (movies / "a.mkv").write_bytes(b"x")
        _sm().set(
            "media_servers",
            [
                {
                    "id": "p1",
                    "type": "plex",
                    "name": "Home",
                    "enabled": True,
                    "libraries": [{"id": "1", "name": "Movies", "remote_paths": [str(movies)]}],
                }
            ],
        )
        return movies

    def test_the_saved_mappings_are_tested_and_a_pass_is_remembered(self, client, nodes, tmp_path):
        movies = self._library(tmp_path)
        rows = [{"app_prefix": str(tmp_path / "media"), "node_prefix": "/mnt"}]
        node_id = _add(client, path_mappings=rows).get_json()["id"]
        resp = client.post(f"/api/nodes/{node_id}/test-access", json={})
        body = resp.get_json()
        assert resp.status_code == 200
        lib = body["libraries"][0]
        assert (lib["library"], lib["app_root"], lib["checked"], lib["ok"]) == ("Movies", str(movies), 1, True)
        assert reg.get_registry().details(node_id).access_result == body

    def test_unsaved_mappings_are_tested_but_not_remembered(self, client, nodes, tmp_path):
        self._library(tmp_path)
        node_id = _add(client).get_json()["id"]
        posted = [{"app_prefix": str(tmp_path), "node_prefix": "/elsewhere"}]
        resp = client.post(f"/api/nodes/{node_id}/test-access", json={"path_mappings": posted})
        assert resp.status_code == 200
        assert nodes("http://nuc:9494").access_requests[0][0][0].node_prefix == "/elsewhere"
        assert reg.get_registry().details(node_id).access_result is None

    def test_bad_mappings_are_refused(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.post(f"/api/nodes/{node_id}/test-access", json={"path_mappings": [{"app_prefix": "x"}]})
        assert resp.status_code == 400

    def test_a_node_that_cant_be_asked_is_502(self, client, nodes, tmp_path, monkeypatch):
        self._library(tmp_path)
        node_id = _add(client).get_json()["id"]

        def boom(mappings, files):
            raise NodeUnreachableError("down")

        monkeypatch.setattr(nodes("http://nuc:9494"), "access_check", boom)
        resp = client.post(f"/api/nodes/{node_id}/test-access", json={})
        assert resp.status_code == 502 and "down" in resp.get_json()["error"]


class TestHealthAndLog:
    def test_health_is_the_setup_health_envelope(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        client.post(f"/api/nodes/{node_id}/check")
        body = client.get(f"/api/nodes/{node_id}/health").get_json()
        assert set(body) == {"overall_ok", "sections"}
        assert body["sections"][0]["id"] == "node_connection"

    @pytest.fixture
    def app_in_sydney(self, monkeypatch):
        monkeypatch.setenv("TZ", "Australia/Sydney")
        time.tzset()
        yield
        monkeypatch.undo()
        time.tzset()

    def test_log_lines(self, client, nodes, app_in_sydney):
        """The node's lines come back in this app's time zone, as the app's own log reads, whatever the node's is:
        the lab's UTC node showed 22:45 beside the app's 09:45."""
        node_id = _add(client).get_json()["id"]
        nodes("http://nuc:9494").log_answer = {
            "lines": [
                # A node that sends ts: read from it, not from its own clock text.
                {"time": "2026-10-15T09:00:00+00:00", "ts": 1792058400.0, "level": "INFO", "message": "one"},
                # An older node, time only: read with its offset.
                {"time": "2026-10-15T09:00:00+00:00", "level": "INFO", "message": "old node"},
                {"time": "not a time", "level": "INFO", "message": "garbled"},
                "two",
            ]
        }
        resp = client.get(f"/api/nodes/{node_id}/log?lines=5")
        assert resp.get_json() == {
            "lines": [
                {"time": "2026-10-15T21:00:00+11:00", "level": "INFO", "message": "one"},
                {"time": "2026-10-15T20:00:00+11:00", "level": "INFO", "message": "old node"},
                {"time": "not a time", "level": "INFO", "message": "garbled"},
                {"time": "", "level": "", "message": "two"},
            ]
        }

    def test_log_errors(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        assert client.get(f"/api/nodes/{node_id}/log?lines=x").status_code == 400
        nodes("http://nuc:9494").log_answer = NodeProtocolError("old helper")
        assert client.get(f"/api/nodes/{node_id}/log").status_code == 502


def test_every_route_needs_auth_once_setup_is_done(client, nodes):
    _sm().complete_setup()
    assert client.get("/api/nodes").status_code == 401
    assert client.post("/api/nodes/probe", json={}).status_code == 401
