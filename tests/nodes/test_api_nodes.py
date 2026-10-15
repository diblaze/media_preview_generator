"""The Nodes page REST API (spec §10.4), through the Flask test client."""

from __future__ import annotations

import http.server
import re
import threading
import time
from pathlib import Path

import pytest

from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.client import (
    NodeAuthError,
    NodeProtocolError,
    NodeRateLimitedError,
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
    "checking",
    "path_mappings",
    "used_by",
    "to_fix",
    "edit_tag",
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
    # Signed in after setup: every /api/nodes route needs a session or the API token (TestAuth).
    _sm().complete_setup()
    with client.session_transaction() as session:
        session["authenticated"] = True
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


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _rows(client):
    return client.get("/api/nodes").get_json()["nodes"]


def _row(client, node_id):
    return next(row for row in _rows(client) if row["id"] == node_id)


def _checked(client, node_id):
    """Check now, then the node's row once that check is done: it runs in the background.

    A check still running when it is asked (the one adding the node starts) would answer it instead, so that one
    finishes first.
    """
    assert _wait_for(lambda: not _row(client, node_id)["checking"])
    resp = client.post(f"/api/nodes/{node_id}/check")
    assert resp.status_code == 202, resp.get_json()
    assert _wait_for(lambda: not _row(client, node_id)["checking"])
    return _row(client, node_id)


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
            "media_setup": {"servers": 0, "libraries": 0},
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

    def test_probe_of_a_plex_database_node_shows_its_plex_database_roles_version(self, client, nodes):
        answer = _info(version="0.0.0.dev0")
        answer["node"]["roles"] = ["plex_db"]
        del answer["worker"]
        answer["agent"] = {"version": "1.2.0", "protocols": [1], "capabilities": []}
        nodes("http://nuc:9494").info_answer = answer
        info = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert info["node"]["version"] == "1.2.0"

    def test_probe_suggests_one_row_per_library_folder(self, client, nodes, tmp_path):
        movies = TestTestAccess()._library(tmp_path)
        info = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert info["suggested_path_mappings"] == [{"app_prefix": str(movies), "node_prefix": str(movies)}]

    @pytest.mark.parametrize(("node_build", "match"), [("b" * 40, False), ("a" * 7, True), ("", True)])
    def test_probe_compares_the_build_of_the_same_version(self, client, nodes, monkeypatch, node_build, match):
        monkeypatch.setenv("GIT_SHA", "a" * 40)
        answer = _info()
        answer["node"]["build"] = node_build
        nodes("http://nuc:9494").info_answer = answer
        info = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert info["version_match"] is match

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

    def test_probe_that_nothing_answered_says_why(self, client, nodes):
        nodes("http://nuc:9494").info_answer = NodeUnreachableError("x", reason="refused")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["error"]["kind"] == "unreachable" and body["error"]["reason"] == "refused"

    @pytest.mark.parametrize(
        ("url", "reason", "advice"),
        [
            ("https://nuc:9494", "tls_certificate", "REQUESTS_CA_BUNDLE"),
            ("https://nuc:9494", "timeout", "use http://"),
            ("http://nuc:9494", "dns", "Can't find nuc."),
        ],
        ids=["untrusted-certificate", "https-timeout", "unknown-host"],
    )
    def test_probe_that_nothing_answered_says_what_to_check(self, client, nodes, url, reason, advice):
        # The node's card words an offline node the same way (unreachable_advice), so the two can't drift apart.
        nodes(url).info_answer = NodeUnreachableError("x", reason=reason)
        body = client.post("/api/nodes/probe", json={"url": url, "key": "k"}).get_json()
        assert advice in body["error"]["advice"], body["error"]

    def test_probe_of_a_node_holding_this_address_back_says_so(self, client, nodes):
        # It answered (429): Add node must not word it as "Nothing answered at this address".
        nodes("http://nuc:9494").info_answer = NodeRateLimitedError("http://nuc:9494 asks this app to wait")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["error"]["kind"] == "unreachable" and body["error"]["reason"] == "rate_limited"

    def test_probe_of_this_apps_own_address_says_it_is_this_app(self, client, nodes):
        own = client.application.config["APP_INSTANCE_ID"]
        nodes("http://nuc:9494").info_answer = NodeProtocolError("app", app_instance=own)
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["error"] == {
            "kind": "protocol",
            "message": "This address is this app itself, not a node.",
            "reason": "this_app",
        }

    def test_probe_of_another_copy_of_this_app_says_so(self, client, nodes):
        nodes("http://nuc:9494").info_answer = NodeProtocolError("app", app_instance="someone-else")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["error"]["reason"] == "other_app"

    def test_probe_of_a_web_page_that_is_no_plex_helper_says_web_page(self, client, nodes, monkeypatch):
        monkeypatch.setattr(api_nodes, "_legacy_ping", lambda url, key: ("unreachable", ""))
        nodes("http://nuc:9494").info_answer = NodeProtocolError("html", web_page=True)
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["error"]["reason"] == "web_page"

    def test_probe_of_something_else_that_answers_says_it_is_not_a_node(self, client, nodes, monkeypatch):
        # An Emby or Jellyfin address: "update the node" would send the user the wrong way.
        monkeypatch.setattr(api_nodes, "_legacy_ping", lambda url, key: ("unreachable", ""))
        nodes("http://nuc:9494").info_answer = NodeProtocolError("404")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["error"] == {
            "kind": "protocol",
            "message": "Something else answers at this address, not a node.",
            "reason": "not_a_node",
        }

    def test_probe_of_a_plex_helper_on_another_protocol_stays_a_version_problem(self, client, nodes, monkeypatch):
        monkeypatch.setattr(api_nodes, "_legacy_ping", lambda url, key: ("incompatible", "0.3.0"))
        nodes("http://nuc:9494").info_answer = NodeProtocolError("404")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["error"]["kind"] == "protocol" and "reason" not in body["error"]

    def test_this_apps_answers_carry_its_instance_id(self, client, nodes):
        resp = client.get("/v1/info")
        assert resp.headers["X-MPG-App-Instance"] == client.application.config["APP_INSTANCE_ID"]

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

    def test_probe_with_the_mask_at_its_own_address_with_a_trailing_slash_uses_the_stored_key(
        self, client, nodes, monkeypatch
    ):
        node_id = _add(client).get_json()["id"]
        asked = []
        monkeypatch.setattr(api_nodes, "_make_client", lambda url, key: asked.append((url, key)) or nodes(url))
        body = {"url": "http://nuc:9494/", "key": "****", "id": node_id}
        assert client.post("/api/nodes/probe", json=body).get_json()["ok"] is True
        assert asked == [("http://nuc:9494", "node-key")]

    def test_probe_with_the_mask_at_another_address_never_sends_the_stored_key(self, client, nodes, monkeypatch):
        node_id = _add(client).get_json()["id"]
        asked, pinged = [], []
        monkeypatch.setattr(api_nodes, "_make_client", lambda url, key: asked.append((url, key)) or nodes(url))
        monkeypatch.setattr(api_nodes, "_legacy_ping", lambda url, key: pinged.append((url, key)) or ("connected", ""))
        body = {"url": "http://attacker:9494", "key": "****", "id": node_id}
        resp = client.post("/api/nodes/probe", json=body)
        assert resp.status_code == 400
        assert resp.get_json()["error"] == api_nodes.KEY_FOR_NEW_ADDRESS
        assert asked == [] and pinged == []

    def test_probe_of_another_address_with_a_typed_key_sends_that_key(self, client, nodes, monkeypatch):
        node_id = _add(client).get_json()["id"]
        asked = []
        monkeypatch.setattr(api_nodes, "_make_client", lambda url, key: asked.append((url, key)) or nodes(url))
        body = {"url": "http://moved:9494", "key": "typed-key", "id": node_id}
        assert client.post("/api/nodes/probe", json=body).status_code == 200
        assert asked == [("http://moved:9494", "typed-key")]

    def test_probe_of_a_saved_node_that_was_reinstalled_with_a_new_id_answers_ok(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        nodes("http://nuc:9494").info_answer = _info(uid="uid-reinstalled")
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "****", "id": node_id}).get_json()
        assert body["ok"] is True and body["info"]["already_added"] is None

    def test_probe_of_a_node_on_another_node_protocol_names_both_versions(self, client, nodes, monkeypatch):
        node = {"id": "uid-1", "name": "nuc", "version": "9.10.0", "protocols": [2], "roles": ["worker"]}

        class OtherProtocol:
            def info(self):
                raise NodeProtocolError("another protocol", node=node)

        pinged = []
        monkeypatch.setattr(api_nodes, "_make_client", lambda url, key: OtherProtocol())
        monkeypatch.setattr(api_nodes, "_legacy_ping", lambda url, key: pinged.append(url) or ("connected", "1"))
        body = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()
        assert body["ok"] is False and body["error"]["kind"] == "protocol"
        assert body["error"]["node_version"] == "9.10.0" and "9.10.0" in body["error"]["message"]
        assert pinged == []


class _NotANode(http.server.BaseHTTPRequestHandler):
    """What Emby or Jellyfin answers at a path it doesn't have: 404 in plain text."""

    def _answer(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = _answer  # noqa: N815
    do_POST = _answer  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


def test_probe_of_a_media_server_address_says_it_is_not_a_node(client):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NotANode)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with client.application.app_context():
            answer = api_nodes._probe(f"http://127.0.0.1:{server.server_address[1]}", "k" * 32)
    finally:
        server.shutdown()
    assert answer["error"]["reason"] == "not_a_node"


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

    @pytest.mark.parametrize(
        ("fields", "error"),
        [
            ({"name": ["nuc"]}, "The node's name must be text"),
            ({"url": ["http://nuc:9494"]}, "The node's address must be text"),
            ({"key": 5}, "The node's key must be text"),
            ({"key": {"k": "v"}}, "The node's key must be text"),
        ],
        ids=["name-list", "url-list", "key-number", "key-object"],
    )
    def test_a_name_address_or_key_that_isnt_text_is_refused_not_turned_into_text(self, client, nodes, fields, error):
        resp = _add(client, **fields)
        assert (resp.status_code, resp.get_json()["error"]) == (400, error)
        assert _sm().nodes == []

    def test_a_probe_with_a_key_that_isnt_text_is_refused(self, client, nodes):
        resp = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": 12345678})
        assert resp.status_code == 400

    def test_a_duplicate_name_is_refused(self, client, nodes):
        _add(client)
        nodes("http://other:9494").info_answer = _info(uid="uid-2")
        resp = _add(client, url="http://other:9494", name="NUC")
        assert resp.status_code == 400 and "already called" in resp.get_json()["error"]


class TestCheckNow:
    """Check now answers at once: the checks run in the background and the page reads the list until they're done."""

    @pytest.fixture
    def real_gaps(self, nodes):
        """The registry with the real wait between a check's attempts (the shared fixture's has none)."""
        registry = reg.NodeRegistry(
            client_factory=lambda record: nodes(record["url"]),
            legacy_ping=lambda url, key: ("unreachable", ""),
            quick_access=lambda client, mappings: True,
            app_version=lambda: APP_VERSION,
        )
        reg.set_registry(registry)
        yield nodes
        registry.stop()  # ends a wait before the next attempt

    def test_check_now_of_a_node_that_does_not_answer_answers_at_once(self, client, real_gaps):
        node_id = _add(client).get_json()["id"]
        assert _wait_for(lambda: not _row(client, node_id)["checking"])
        real_gaps("http://nuc:9494").info_answer = NodeUnreachableError("http://nuc:9494 didn't answer: ConnectTimeout")

        started = time.monotonic()
        resp = client.post(f"/api/nodes/{node_id}/check")
        took = time.monotonic() - started

        # Its three attempts 4 s apart held this web server thread 8 s (17-38 s with real timeouts).
        assert took < 1.0, f"POST /check held the request for {took:.1f} s"
        assert resp.status_code == 202 and resp.get_json()["checking"] is True
        assert _row(client, node_id)["checking"] is True
        reg.get_registry().stop()
        assert _wait_for(lambda: not _row(client, node_id)["checking"])
        assert _row(client, node_id)["state"] == "offline"

    def test_check_all_checks_every_turned_on_node_once_in_the_background(self, client, nodes):
        ids = {}
        for name, uid in (("nuc", "uid-1"), ("pc", "uid-2"), ("old", "uid-3")):
            nodes(f"http://{name}:9494").info_answer = _info(uid=uid)
            ids[name] = _add(client, name=name, url=f"http://{name}:9494").get_json()["id"]
        assert client.put(f"/api/nodes/{ids['old']}", json={"enabled": False}).status_code == 200
        assert _wait_for(lambda: not any(row["checking"] for row in _rows(client)))
        release, asked = threading.Event(), []
        for name in ids:
            fake = nodes(f"http://{name}:9494")

            def slow(name=name, answer=fake.info_answer):
                asked.append(name)
                release.wait(5)
                return answer

            fake.info = slow

        resp = client.post("/api/nodes/check")
        again = client.post("/api/nodes/check")  # while those checks still run

        body = resp.get_json()
        assert (resp.status_code, again.status_code, body["app_version"]) == (202, 202, APP_VERSION)
        assert {row["name"]: row["checking"] for row in body["nodes"]} == {"nuc": True, "pc": True, "old": False}
        release.set()
        assert _wait_for(lambda: not any(row["checking"] for row in _rows(client)))
        assert sorted(asked) == ["nuc", "pc"]
        assert {row["name"]: row["state"] for row in _rows(client)} == {
            "nuc": "ready",
            "pc": "ready",
            "old": "disabled",
        }


class TestRowState:
    def test_check_now_leaves_the_live_state_and_last_info_in_the_row(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        row = _checked(client, node_id)
        assert row["state"] == "ready" and row["checking"] is False
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

    def test_rescan_gpus_on_a_node_that_does_not_answer_is_a_502_not_the_old_gpus(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        assert _checked(client, node_id)["state"] == "ready"
        fake = nodes("http://nuc:9494")

        def down():
            raise NodeUnreachableError("http://nuc:9494 didn't answer (connection refused)")

        fake.rescan_gpus = down
        fake.info_answer = NodeUnreachableError("http://nuc:9494 didn't answer (connection refused)")
        resp = client.post(f"/api/nodes/{node_id}/rescan-gpus")
        assert resp.status_code == 502
        assert resp.get_json()["error"].startswith("Couldn't ask nuc:")

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
        """The page lists only saved nodes: one it asks about was removed since (it said "No node with id <hex>")."""
        resp = getattr(client, method)(f"/api/nodes/nope{path}", json={})
        assert resp.status_code == 404
        assert resp.get_json() == {"error": "This node was removed in another tab."}


class TestUpdate:
    def test_a_rename_with_the_mask_keeps_the_key(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.put(f"/api/nodes/{node_id}", json={"name": "renamed", "key": "****", "id": "evil"})
        assert resp.status_code == 200 and resp.get_json()["name"] == "renamed"
        stored = _sm().nodes[0]
        assert (stored["id"], stored["key"], stored["node_uid"]) == (node_id, "node-key", "uid-1")

    def test_a_new_address_goes_back_to_connecting(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        _checked(client, node_id)
        nodes("http://moved:9494").info_answer = NodeUnreachableError("slow")
        row = client.put(f"/api/nodes/{node_id}", json={"url": "http://moved:9494", "key": "node-key"}).get_json()
        assert row["url"] == "http://moved:9494"
        assert row["state"] in ("connecting", "offline")

    def test_a_new_address_forgets_which_node_answered_at_the_old_one(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        assert _sm().nodes[0]["node_uid"] == "uid-1"
        nodes("http://moved:9494").info_answer = NodeUnreachableError("slow")
        client.put(f"/api/nodes/{node_id}", json={"url": "http://moved:9494", "key": "node-key"})
        assert _sm().nodes[0]["node_uid"] is None

    @pytest.mark.parametrize("key", [None, "****"], ids=["no-key", "mask"])
    def test_a_new_address_without_a_typed_key_is_refused(self, client, nodes, key):
        node_id = _add(client).get_json()["id"]
        body = {"url": "http://attacker:9494", **({"key": key} if key else {})}
        resp = client.put(f"/api/nodes/{node_id}", json=body)
        assert resp.status_code == 400 and resp.get_json()["error"] == api_nodes.KEY_FOR_NEW_ADDRESS
        assert (_sm().nodes[0]["url"], _sm().nodes[0]["node_uid"]) == ("http://nuc:9494", "uid-1")

    def test_the_same_address_with_the_mask_keeps_the_key_and_the_node_id(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.put(f"/api/nodes/{node_id}", json={"url": "http://nuc:9494/", "key": "****"})
        assert resp.status_code == 200
        assert (_sm().nodes[0]["key"], _sm().nodes[0]["node_uid"]) == ("node-key", "uid-1")

    def test_turning_it_off(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        row = client.put(f"/api/nodes/{node_id}", json={"enabled": False}).get_json()
        assert (row["enabled"], row["state"]) == (False, "disabled")

    def test_an_invalid_change_is_refused_and_nothing_is_saved(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.put(f"/api/nodes/{node_id}", json={"url": "nope"})
        assert resp.status_code == 400
        assert _sm().nodes[0]["url"] == "http://nuc:9494"

    def test_a_save_from_a_dialog_opened_before_another_save_is_refused(self, client, nodes):
        """Tab B opened the node, tab A renamed it; B switching it off must not put the old name back."""
        row = _add(client).get_json()
        node_id, opened_with = row["id"], row["edit_tag"]
        tab_a = client.put(f"/api/nodes/{node_id}", json={"name": "nuc-gpu", "key": "****", "edit_tag": opened_with})
        assert tab_a.status_code == 200 and tab_a.get_json()["edit_tag"] != opened_with

        tab_b = client.put(
            f"/api/nodes/{node_id}", json={"name": "nuc", "key": "****", "enabled": False, "edit_tag": opened_with}
        )

        assert tab_b.status_code == 409
        assert tab_b.get_json() == {"error": api_nodes.CHANGED_ELSEWHERE}
        assert (_sm().nodes[0]["name"], _sm().nodes[0]["enabled"]) == ("nuc-gpu", True)

    def test_a_save_after_the_app_recorded_the_nodes_id_or_another_node_changed_goes_through(self, client, nodes):
        """Only an edit of this node's own fields makes a dialog stale; the app's own records don't."""
        nodes("http://nuc:9494").info_answer = NodeUnreachableError("down")
        row = _add(client).get_json()
        nodes("http://other:9494").info_answer = _info(uid="uid-2")
        other = _add(client, name="other", url="http://other:9494").get_json()
        nodes("http://nuc:9494").info_answer = _info()
        assert _checked(client, row["id"])["state"] == "ready"
        assert _sm().nodes[0]["node_uid"] == "uid-1"
        assert client.put(f"/api/nodes/{other['id']}", json={"name": "other-2", "key": "****"}).status_code == 200

        resp = client.put(f"/api/nodes/{row['id']}", json={"enabled": False, "edit_tag": row["edit_tag"]})

        assert resp.status_code == 200 and resp.get_json()["enabled"] is False

    def test_a_save_without_a_tag_is_not_checked(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        client.put(f"/api/nodes/{node_id}", json={"name": "nuc-gpu", "key": "****"})
        assert client.put(f"/api/nodes/{node_id}", json={"enabled": False}).status_code == 200

    def test_the_tag_never_carries_the_key(self, client, nodes):
        row = _add(client).get_json()
        client.put(f"/api/nodes/{row['id']}", json={"key": "another-key"})
        assert _row(client, row["id"])["edit_tag"] == row["edit_tag"]

    @pytest.mark.parametrize(
        ("change", "forgotten"),
        [
            ({"url": "http://nuc2:9494", "key": "node-key"}, True),
            ({"key": "another-key"}, True),
            ({"enabled": False}, True),
            ({"name": "renamed"}, False),
        ],
        ids=["moved", "new-key", "turned-off", "renamed"],
    )
    def test_a_change_of_address_key_or_switch_forgets_its_servers_intro_and_credits_answers(
        self, client, nodes, change, forgotten
    ):
        """A server names only its node: the node's address lives here, so the server's cached answer can't see it."""
        from media_preview_generator.markers import inspect
        from media_preview_generator.servers.registry import server_config_from_dict

        nodes("http://nuc2:9494")
        node_id = _add(client).get_json()["id"]
        other = {**_plex_server({"enabled": True, "node_id": "another-node"}), "id": "p2"}
        _sm().set("media_servers", [_plex_server({"enabled": True, "node_id": node_id}), other])
        inspect.clear_capability_cache()
        configs = [server_config_from_dict(entry) for entry in _sm().get("media_servers")]
        for config in configs:
            inspect._CAPABILITY_CACHE.get(config, "status", lambda: "old node's answer")

        assert client.put(f"/api/nodes/{node_id}", json=change).status_code == 200

        answers = [inspect._CAPABILITY_CACHE.get(config, "status", lambda: "asked again") for config in configs]
        assert answers == ["asked again" if forgotten else "old node's answer", "old node's answer"]
        inspect.clear_capability_cache()


class TestDelete:
    def test_a_list_that_wont_save_is_a_400_not_a_500(self, client, nodes, monkeypatch):
        node_id = _add(client).get_json()["id"]

        def refuse(nodes_list, expected_revision=None):
            raise ValueError("The node's name must be at most 80 characters")

        monkeypatch.setattr(type(_sm()), "update_nodes", lambda self, n, expected_revision=None: refuse(n))
        resp = client.delete(f"/api/nodes/{node_id}")
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "The node's name must be at most 80 characters"}

    def test_each_row_counts_its_setup_health_rows_to_fix(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        reg.get_registry().check_now(node_id)
        row = next(n for n in client.get("/api/nodes").get_json()["nodes"] if n["id"] == node_id)
        assert row["state"] == "ready"
        # Not tested and in no worker group: two recommendations, nothing that stops it working.
        assert row["to_fix"] == {"must_fix": 0, "recommended": 2}

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
                    "markers": {"plex": {"agent": {"enabled": True, "node_id": node_id}}},
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

    def test_a_node_removed_while_a_server_is_saved_onto_it_never_leaves_the_server_on_it(
        self, client, nodes, monkeypatch
    ):
        """The server save checks its chosen node before taking the settings lock; a Remove landing in between
        must make one of the two lose."""
        import threading

        from media_preview_generator.markers import settings as markers_settings

        nodes("http://nuc:9494").info_answer["node"]["roles"] = ["plex_db"]
        node_id = _add(client).get_json()["id"]
        plex = {"db_write_confirmed_at": "2026-01-01", "agent": {"enabled": False, "node_id": None}}
        server = {
            **_plex_server({}),
            "enabled": True,
            "url": "http://plex:32400",
            "auth": {"method": "token", "token": "t" * 20},
            "libraries": [],
            "markers": {"enabled": True, "library_ids": None, "plex": plex},
        }
        _sm().set("media_servers", [server])
        other = client.application.test_client()
        with other.session_transaction() as session:
            session["authenticated"] = True
        removed: list[int] = []
        check = markers_settings._node_choice_error

        def check_then_remove(chosen: str) -> str:
            error = check(chosen)
            if not removed:
                remove = threading.Thread(
                    target=lambda: removed.append(other.delete(f"/api/nodes/{chosen}").status_code)
                )
                remove.start()
                remove.join(10)
            return error

        monkeypatch.setattr(markers_settings, "_node_choice_error", check_then_remove)
        chosen = {**server["markers"], "plex": {**plex, "agent": {"enabled": True, "node_id": node_id}}}
        resp = client.put("/api/servers/p1", json={"markers": chosen})

        assert removed == [200] and _sm().nodes == []
        assert resp.status_code == 400 and "isn't on the Nodes page any more" in resp.get_json()["error"]
        assert _sm().get("media_servers")[0]["markers"]["plex"]["agent"]["enabled"] is False


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

    def test_a_pass_of_mappings_replaced_during_the_test_is_not_remembered(self, client, nodes, tmp_path, monkeypatch):
        self._library(tmp_path)
        rows = [{"app_prefix": str(tmp_path / "media"), "node_prefix": "/mnt"}]
        node_id = _add(client, path_mappings=rows).get_json()["id"]
        node = nodes("http://nuc:9494")
        ask = node.access_check
        replaced = [{"app_prefix": str(tmp_path / "media"), "node_prefix": "/volume1"}]

        def saved_while_the_node_checks(mappings, files):
            _sm().update_nodes([{**n, "path_mappings": replaced} for n in _sm().nodes])
            return ask(mappings, files)

        monkeypatch.setattr(node, "access_check", saved_while_the_node_checks)
        resp = client.post(f"/api/nodes/{node_id}/test-access", json={})
        assert resp.status_code == 200 and resp.get_json()["libraries"][0]["ok"] is True
        assert reg.get_registry().details(node_id).access_result is None

    def test_bad_mappings_are_refused(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        resp = client.post(f"/api/nodes/{node_id}/test-access", json={"path_mappings": [{"app_prefix": "x"}]})
        assert resp.status_code == 400

    def test_servers_without_libraries_are_told_apart_from_no_server(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        _sm().set("media_servers", [{"id": "p1", "type": "plex", "name": "Home", "enabled": True, "libraries": []}])
        body = client.post(f"/api/nodes/{node_id}/test-access", json={}).get_json()
        assert (body["libraries"], body["media_setup"]) == ([], {"servers": 1, "libraries": 0})
        probe = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_json()["info"]
        assert probe["media_setup"] == {"servers": 1, "libraries": 0}

    def test_a_node_added_before_any_library_gets_rows_suggested_later(self, client, nodes, tmp_path):
        node_id = _add(client).get_json()["id"]
        before = client.get(f"/api/nodes/{node_id}/suggested-mappings").get_json()
        assert before == {"path_mappings": [], "media_setup": {"servers": 0, "libraries": 0}}
        movies = self._library(tmp_path)
        after = client.get(f"/api/nodes/{node_id}/suggested-mappings").get_json()
        assert after == {
            "path_mappings": [{"app_prefix": str(movies), "node_prefix": str(movies)}],
            "media_setup": {"servers": 1, "libraries": 1},
        }
        assert _sm().nodes[0]["path_mappings"] == []
        assert client.get("/api/nodes/nope/suggested-mappings").status_code == 404

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
        _checked(client, node_id)
        body = client.get(f"/api/nodes/{node_id}/health").get_json()
        assert set(body) == {"overall_ok", "sections"}
        assert body["sections"][0]["id"] == "node_connection"

    def test_another_machine_at_a_plex_database_nodes_address_is_a_must_fix_not_its_database(
        self, client, nodes, monkeypatch
    ):
        """Another machine given the node's address and key showed its own Plex database as this node's."""
        import json
        from unittest.mock import MagicMock

        from media_preview_generator.markers.publishers.base import Capability, CapabilityReport
        from media_preview_generator.markers.publishers.plex_remote import report_to_json
        from media_preview_generator.nodes import readiness
        from media_preview_generator.nodes.protocol import KIND_WRONG_NODE, NODE_UID_HEADER

        def other_machine(session, url, *, headers, **kwargs):
            # Its own database is fine; work that names another node's id, it refuses.
            agent = {"version": "1.2.0", "protocols": [1, 2]}
            if headers.get(NODE_UID_HEADER, "uid-2") != "uid-2":
                status, body = 409, {"ok": False, "agent": agent, "error": {"kind": KIND_WRONG_NODE}}
            else:
                report = CapabilityReport(Capability.READY, "", {"db_path": "/b/db", "fs_type": "ext4"})
                status, body = 200, {"ok": True, "agent": agent, "result": {"report": report_to_json(report)}}
            answer = MagicMock(status_code=status, headers={})
            answer.iter_content.return_value = [json.dumps(body).encode()]
            return answer

        monkeypatch.setattr(readiness, "_last_reports", {})
        monkeypatch.setattr("requests.Session.post", other_machine)
        nodes("http://nuc:9494").info_answer["node"]["roles"] = ["plex_db"]
        node_id = _add(client).get_json()["id"]
        _checked(client, node_id)
        assert _sm().nodes[0]["node_uid"] == "uid-1"

        body = client.get(f"/api/nodes/{node_id}/health").get_json()

        assert body["overall_ok"] is False
        rows = next(s for s in body["sections"] if s["id"] == "node_plex_db")["checks"]
        assert [(r["id"], r["ok"]) for r in rows] == [("plex_db_reachable", False)]
        assert "isn't the Plex database node saved there" in rows[0]["reason"]

    def test_a_plex_database_must_fix_its_setup_health_found_is_counted_on_its_card(self, client, nodes, monkeypatch):
        """The card said Ready with nothing to fix while Setup Health said the node can't write Plex's database."""
        from media_preview_generator.markers.publishers import plex_remote
        from media_preview_generator.markers.publishers.base import Capability, CapabilityReport
        from media_preview_generator.nodes import readiness

        class CantWrite:
            def __init__(self, url, key, **kwargs):
                pass

            def file_checks(self, *, deadline):
                return CapabilityReport(
                    Capability.MISCONFIGURED, "can't write", {"db_path": "/d", "unwritable": ["/d"]}
                )

        monkeypatch.setattr(readiness, "_last_reports", {})
        monkeypatch.setattr(plex_remote, "RemotePlexDb", CantWrite)
        nodes("http://nuc:9494").info_answer["node"]["roles"] = ["plex_db"]
        node_id = _add(client).get_json()["id"]
        _checked(client, node_id)

        def card() -> dict:
            return next(n for n in client.get("/api/nodes").get_json()["nodes"] if n["id"] == node_id)

        assert card()["to_fix"] == {"must_fix": 0, "recommended": 0}
        assert client.get(f"/api/nodes/{node_id}/health").get_json()["overall_ok"] is False
        assert card()["to_fix"] == {"must_fix": 1, "recommended": 0}

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

    def test_a_log_line_with_a_time_outside_what_a_date_holds_keeps_the_log_tab_working(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        nodes("http://nuc:9494").log_answer = {
            "lines": [
                {"time": "", "ts": 1e20, "level": "INFO", "message": "far future"},
                {"time": "", "ts": 253402300800, "level": "INFO", "message": "year 10000"},
                {"time": "", "ts": 10**400, "level": "INFO", "message": "too big for a float"},
                {"time": "0001-01-01T00:00:00-12:00", "level": "INFO", "message": "year 1 in the app's zone"},
            ]
        }
        resp = client.get(f"/api/nodes/{node_id}/log")
        assert resp.status_code == 200
        assert [line["message"] for line in resp.get_json()["lines"]] == [
            "far future",
            "year 10000",
            "too big for a float",
            "year 1 in the app's zone",
        ]

    def test_log_errors(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        assert client.get(f"/api/nodes/{node_id}/log?lines=x").status_code == 400
        nodes("http://nuc:9494").log_answer = NodeProtocolError("other protocol", node={"version": "8.0.0"})
        resp = client.get(f"/api/nodes/{node_id}/log")
        assert resp.status_code == 502 and resp.get_json()["error"] == "Couldn't read nuc's log: other protocol"

    def test_the_log_of_an_older_plex_helper_says_it_cant_share_one_in_plain_words(self, client, nodes):
        """Not a failure: the Log tab of every older helper logged a 502 in the browser console."""
        node_id = _add(client).get_json()["id"]
        refusal = "This Plex marker agent is version 1.0.0 and speaks protocol 1; the app asked for none."
        nodes("http://nuc:9494").log_answer = NodeProtocolError(refusal)
        resp = client.get(f"/api/nodes/{node_id}/log")
        assert resp.status_code == 200
        assert resp.get_json() == {
            "lines": [],
            "message": "This Plex database node is an older release and can't share its log. Update it to see its "
            "log here.",
        }


class TestAuth:
    """Every route sends stored keys out or reaches nodes, so none is open, not even before setup is complete."""

    ROUTES = [
        ("get", "/api/nodes"),
        ("post", "/api/nodes/probe"),
        ("post", "/api/nodes"),
        ("put", "/api/nodes/n1"),
        ("delete", "/api/nodes/n1"),
        ("post", "/api/nodes/n1/check"),
        ("post", "/api/nodes/check"),
        ("post", "/api/nodes/n1/test-access"),
        ("get", "/api/nodes/n1/health"),
        ("get", "/api/nodes/n1/log"),
        ("post", "/api/nodes/n1/rescan-gpus"),
    ]

    @pytest.mark.parametrize("setup_complete", [False, True], ids=["before-setup", "after-setup"])
    @pytest.mark.parametrize(("method", "path"), ROUTES)
    def test_an_unauthenticated_caller_is_refused(self, client, monkeypatch, setup_complete, method, path):
        client.application.config["WTF_CSRF_ENABLED"] = False
        monkeypatch.setattr(type(_sm()), "is_setup_complete", lambda self: setup_complete)
        monkeypatch.setattr(api_nodes, "_make_client", lambda url, key: pytest.fail("a node was asked"))
        body = {"id": "n1", "url": "http://attacker:9494", "key": "****"}
        resp = getattr(client, method)(path, json=body) if method != "get" else client.get(path)
        assert resp.status_code == 401

    def test_the_api_token_is_accepted_before_setup(self, client, nodes, monkeypatch, auth_headers):
        monkeypatch.setattr(type(_sm()), "is_setup_complete", lambda self: False)
        fresh = client.application.test_client()
        assert fresh.get("/api/nodes", headers=auth_headers).status_code == 200


def _plex_server(agent: dict) -> dict:
    return {"id": "p1", "type": "plex", "name": "Home", "markers": {"plex": {"agent": agent}}}


class TestSwitchedOffPlexDatabaseNode:
    """A server whose Plex database node is switched off says this app opens the database: it doesn't use the node."""

    def test_it_doesnt_keep_the_node_from_being_removed(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        _sm().set("media_servers", [_plex_server({"enabled": False, "node_id": node_id})])
        assert client.get("/api/nodes").get_json()["nodes"][0]["used_by"]["servers"] == []

        resp = client.delete(f"/api/nodes/{node_id}")

        assert resp.status_code == 200, resp.get_json()
        assert _sm().nodes == []
        # Switching it on again asks for a node instead of pointing at one that is gone.
        assert _sm().get("media_servers")[0]["markers"]["plex"]["agent"] == {"enabled": False, "node_id": None}

    def test_removing_it_forgets_its_servers_intro_and_credits_answers(self, client, nodes):
        """Pointed at a node with the same id again (a restored backup), the server must not read the gone node's
        cached answer."""
        from media_preview_generator.markers import inspect
        from media_preview_generator.servers.registry import server_config_from_dict

        node_id = _add(client).get_json()["id"]
        other = {**_plex_server({"enabled": False, "node_id": "another-node"}), "id": "p2"}
        _sm().set("media_servers", [_plex_server({"enabled": False, "node_id": node_id}), other])
        inspect.clear_capability_cache()
        configs = [server_config_from_dict(entry) for entry in _sm().get("media_servers")]
        for config in configs:
            inspect._CAPABILITY_CACHE.get(config, "status", lambda: "gone node's answer")

        assert client.delete(f"/api/nodes/{node_id}").status_code == 200

        answers = [inspect._CAPABILITY_CACHE.get(config, "status", lambda: "asked again") for config in configs]
        assert answers == ["asked again", "gone node's answer"]
        inspect.clear_capability_cache()

    def test_a_v24_migrated_switched_off_helper_can_be_removed(self, client, nodes):
        from media_preview_generator.upgrade import _migrate_schema

        agent = {"enabled": False, "url": "http://old-helper:9494", "token": "k"}
        markers = {
            "enabled": True,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": "2026-01-01", "on_plex_redetect": "restore", "agent": agent},
        }
        _sm().apply_changes(
            updates={"_schema_version": 23, "media_servers": [{**_plex_server({}), "markers": markers}]}
        )
        _migrate_schema(_sm())

        resp = client.delete(f"/api/nodes/{_sm().nodes[0]['id']}")

        assert resp.status_code == 200, resp.get_json()


class TestReinstalledNode:
    """The saved key at the saved address is what makes a node this node; a new id means it was reinstalled."""

    def _plex_db_info(self, uid: str, machine_identifier: str = "plex-a") -> dict:
        answer = _info(uid=uid)
        answer["node"]["roles"] = ["plex_db"]
        del answer["worker"]
        answer["plex_db"] = {"config_dir": "/plex", "db_present": True, "machine_identifier": machine_identifier}
        return answer

    def _recreated(self, client, nodes, *, machine_identifier: str, server_identity: str | None = "plex-a"):
        """A Plex database node added, used by a server, then recreated with a new id; answers its node id."""
        helper = nodes("http://plex-host:9494")
        helper.info_answer = self._plex_db_info("first-container")
        node_id = _add(client, url="http://plex-host:9494", name="Plex helper").get_json()["id"]
        server = _plex_server({"enabled": True, "node_id": node_id})
        server["server_identity"] = server_identity
        _sm().set("media_servers", [server])
        assert _sm().nodes[0]["node_uid"] == "first-container"

        # docker pull + rm + run without a kept /config: a fresh node.json, so a new id.
        helper.info_answer = self._plex_db_info("second-container", machine_identifier)
        row = _checked(client, node_id)
        assert row["state"] == "ready", row["reason"]
        return node_id

    def _refusal(self):
        from media_preview_generator.markers.publishers import plex_remote
        from media_preview_generator.markers.settings import load_server

        return plex_remote.node_refusal(load_server(_sm().get("media_servers")[0]["markers"], "plex"))

    def test_a_recreated_plex_database_node_beside_the_same_plex_keeps_working(self, client, nodes):
        self._recreated(client, nodes, machine_identifier="plex-a")

        assert _sm().nodes[0]["node_uid"] == "second-container"
        assert self._refusal() is None

    @pytest.mark.parametrize(
        ("machine_identifier", "server_identity"),
        [("", "plex-a"), ("plex-a", None), ("plex-b", "plex-a")],
        ids=["node-doesnt-say", "server-doesnt-say", "another-plex"],
    )
    def test_a_new_id_that_cant_be_tied_to_the_servers_plex_stops_its_plex_database_work(
        self, client, nodes, machine_identifier, server_identity
    ):
        from media_preview_generator.markers.publishers.plex_remote import AGENT_UNCONFIRMED_NODE

        self._recreated(client, nodes, machine_identifier=machine_identifier, server_identity=server_identity)

        assert _sm().nodes[0]["node_uid"] == "first-container"
        refusal = self._refusal()
        assert refusal is not None and refusal.agent_state == AGENT_UNCONFIRMED_NODE
        assert refusal.message == (
            "Plex helper reports a new id and this app can't confirm it is the same Plex. Type the node's key again "
            "on its Settings tab to confirm."
        )

    def test_typing_the_key_again_confirms_it_and_records_the_new_id(self, client, nodes):
        node_id = self._recreated(client, nodes, machine_identifier="")
        assert self._refusal() is not None

        resp = client.put(f"/api/nodes/{node_id}", json={"key": "node-key"})

        assert resp.status_code == 200, resp.get_json()
        assert self._refusal() is None
        _checked(client, node_id)
        assert _sm().nodes[0]["node_uid"] == "second-container"
        assert self._refusal() is None

    def test_saving_settings_without_typing_the_key_doesnt_confirm_it(self, client, nodes):
        node_id = self._recreated(client, nodes, machine_identifier="")

        assert client.put(f"/api/nodes/{node_id}", json={"name": "Plex box", "key": "****"}).status_code == 200

        assert self._refusal() is not None

    def test_the_new_id_is_logged_once(self, client, nodes):
        from loguru import logger

        helper = nodes("http://nuc:9494")
        node_id = _add(client).get_json()["id"]
        helper.info_answer = _info(uid="uid-reinstalled")
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
        try:
            _checked(client, node_id)
            _checked(client, node_id)
        finally:
            logger.remove(sink)
        assert [line for line in lines if "new id" in line] == ["nuc reports a new id; it was probably reinstalled"]
        assert _sm().nodes[0]["node_uid"] == "uid-reinstalled"


class TestBadAddresses:
    BAD = [
        "http://[::1",
        "http://nuc:99999",
        "http://nuc:abc",
        "http://nuc lan:9494",
        "http://nuc:9494#",
        "http://nuc:9494?",
        "http://nas..lan:9494",
    ]

    @pytest.mark.parametrize("url", BAD)
    def test_probe_add_and_edit_refuse_it_with_a_400(self, client, nodes, url):
        assert client.post("/api/nodes/probe", json={"url": url, "key": "k"}).status_code == 400
        assert _add(client, url=url).status_code == 400
        assert _sm().nodes == []
        node_id = _add(client).get_json()["id"]
        assert client.put(f"/api/nodes/{node_id}", json={"url": url, "key": "new-key"}).status_code == 400
        assert _sm().nodes[0]["url"] == "http://nuc:9494"


class TestTheSameMachineAddedTwice:
    def test_an_address_differing_only_in_letter_case_is_already_added(self, client, nodes):
        nodes("http://nuc:9494").info_answer = NodeUnreachableError("didn't answer")
        nodes("http://NUC:9494").info_answer = NodeUnreachableError("didn't answer")
        assert _add(client, url="http://nuc:9494").status_code == 201
        assert _add(client, name="living room", url="http://NUC:9494").status_code == 409
        assert len(_sm().nodes) == 1

    def test_the_second_record_of_one_machine_is_flagged_and_gets_no_work(self, client, nodes):
        # Added while the node was off (nothing to compare), at two addresses of the one machine.
        for url in ("http://nuc:9494", "http://10.0.0.5:9494"):
            nodes(url).info_answer = NodeUnreachableError("didn't answer")
        first = _add(client, url="http://nuc:9494").get_json()["id"]
        second = _add(client, name="nuc by ip", url="http://10.0.0.5:9494").get_json()["id"]
        registry = reg.get_registry()
        deadline = time.monotonic() + 10
        # Adding a node checks it in the background: let those checks end before the machine starts answering.
        while time.monotonic() < deadline and any(
            registry._nodes[n].checks_begun < 1 or registry._nodes[n].checking for n in (first, second)
        ):
            time.sleep(0.01)
        for url in ("http://nuc:9494", "http://10.0.0.5:9494"):
            nodes(url).info_answer = _info(uid="the-one-machine")
        client.post(f"/api/nodes/{first}/check")
        client.post(f"/api/nodes/{second}/check")

        assert registry.view(first).state == reg.STATE_READY
        assert registry.view(second).state == reg.STATE_DUPLICATE and "Remove one" in registry.view(second).reason
        assert registry.capacity(second).cpu is False
        assert [n.get("node_uid") for n in _sm().nodes] == ["the-one-machine", None]


class TestNodeSentValues:
    def test_a_nan_load_keeps_the_nodes_list_valid_json(self, client, nodes):
        import json

        answer = _info()
        answer["worker"]["cpu"].update({"load1": float("nan"), "ram_total": float("inf")})
        answer["platform"] = {"os": "L" * 5000, "arch": float("nan")}
        nodes("http://nuc:9494").info_answer = answer
        node_id = _add(client).get_json()["id"]
        _checked(client, node_id)

        def refuse(constant):
            raise ValueError(f"not JSON a browser can parse: {constant}")

        body = json.loads(client.get("/api/nodes").get_data(as_text=True), parse_constant=refuse)
        row = body["nodes"][0]
        assert (row["cpu"]["load1"], row["cpu"]["ram_total"], row["cpu"]["cores"]) == (None, None, 8)
        assert row["platform"]["arch"] is None and len(row["platform"]["os"]) == api_nodes.NODE_TEXT_MAX

    def test_a_nan_in_the_probe_answer_keeps_it_valid_json(self, client, nodes):
        import json

        answer = _info()
        answer["worker"]["cpu"]["load1"] = float("nan")
        nodes("http://nuc:9494").info_answer = answer

        def refuse(constant):
            raise ValueError(constant)

        text = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "k"}).get_data(as_text=True)
        assert json.loads(text, parse_constant=refuse)["info"]["worker"]["cpu"]["load1"] is None

    def _good_and_bad(self, client, nodes):
        nodes("http://good:9494").info_answer = _info(uid="uid-good")
        nodes("http://bad:9494").info_answer = _info(uid="uid-bad")
        good = _add(client, name="good", url="http://good:9494").get_json()["id"]
        bad = _add(client, name="bad", url="http://bad:9494").get_json()["id"]
        return good, bad

    @pytest.mark.parametrize("field", ["cores", "busy"])
    def test_an_out_of_range_number_from_one_node_keeps_every_node_listed(self, client, nodes, field):
        import json

        good, bad = self._good_and_bad(client, nodes)
        answer = _info(uid="uid-bad")
        # JSON's 1e400 parses to infinity, which int() refuses.
        target = answer["worker"]["cpu"] if field == "cores" else answer["worker"]
        target[field] = json.loads("1e400")
        nodes("http://bad:9494").info_answer = answer
        client.post(f"/api/nodes/{good}/check")
        client.post(f"/api/nodes/{bad}/check")
        resp = client.get("/api/nodes")
        assert resp.status_code == 200
        assert {row["id"] for row in resp.get_json()["nodes"]} == {good, bad}
        assert {v.id for v in reg.get_registry().views()} == {good, bad}

    def test_a_whole_number_too_big_for_a_float_still_updates_the_nodes_state(self, client, nodes):
        # json.loads makes a 401-digit JSON integer a Python int, and math.isfinite raised OverflowError on it: every
        # check failed "unexpectedly" and a node that had been ready stayed ready, whatever it answered now.
        node = nodes("http://nuc:9494")
        node_id = _add(client).get_json()["id"]
        assert _checked(client, node_id)["state"] == "ready"
        answer = _info(version="1.0.0")
        answer["worker"]["cpu"]["cores"] = 10**400
        node.info_answer = answer

        row = _checked(client, node_id)

        assert row["state"] == reg.STATE_UPDATE_NEEDED
        assert row["cpu"]["cores"] is None

    def test_add_nodes_connect_answers_for_a_whole_number_too_big_for_a_float(self, client, nodes):
        answer = _info()
        answer["worker"]["cpu"]["cores"] = 10**400
        nodes("http://nuc:9494").info_answer = answer

        resp = client.post("/api/nodes/probe", json={"url": "http://nuc:9494", "key": "node-key"})

        assert resp.status_code == 200 and resp.get_json()["info"]["worker"]["cpu"]["cores"] is None

    def test_a_deeply_nested_answer_from_one_node_keeps_every_node_listed(self, client, nodes):
        import json

        good, bad = self._good_and_bad(client, nodes)
        answer = _info(uid="uid-bad")
        answer["extra"] = json.loads("[" * 600 + "]" * 600)
        nodes("http://bad:9494").info_answer = answer
        client.post(f"/api/nodes/{bad}/check")
        resp = client.get("/api/nodes")
        assert resp.status_code == 200
        assert {row["id"] for row in resp.get_json()["nodes"]} == {good, bad}


class TestATestOfRowsSavedLater:
    """Add node and the Media tab test the rows in the editor before saving them: a pass is kept once they are."""

    def _library(self, tmp_path):
        return TestTestAccess._library(self, tmp_path)

    def test_a_pass_in_the_add_node_wizard_is_kept_once_its_rows_are_saved(self, client, nodes, tmp_path):
        self._library(tmp_path)
        rows = [{"app_prefix": str(tmp_path / "media"), "node_prefix": "/mnt"}]
        node_id = _add(client).get_json()["id"]

        tested = client.post(f"/api/nodes/{node_id}/test-access", json={"path_mappings": rows}).get_json()
        assert tested["libraries"][0]["ok"] is True
        assert reg.get_registry().details(node_id).access_result is None
        assert client.put(f"/api/nodes/{node_id}", json={"path_mappings": rows}).status_code == 200

        assert reg.get_registry().details(node_id).access_result == tested
        ids = [c["id"] for s in client.get(f"/api/nodes/{node_id}/health").get_json()["sections"] for c in s["checks"]]
        assert "node_access_untested" not in ids

    def test_a_pass_of_other_rows_is_not_kept(self, client, nodes, tmp_path):
        self._library(tmp_path)
        rows = [{"app_prefix": str(tmp_path / "media"), "node_prefix": "/mnt"}]
        node_id = _add(client).get_json()["id"]
        client.post(f"/api/nodes/{node_id}/test-access", json={"path_mappings": rows})

        saved = [{"app_prefix": str(tmp_path / "media"), "node_prefix": "/volume1"}]
        assert client.put(f"/api/nodes/{node_id}", json={"path_mappings": saved}).status_code == 200

        assert reg.get_registry().details(node_id).access_result is None


class TestMovingOntoAnotherNodesAddress:
    def test_an_address_another_saved_node_uses_is_refused(self, client, nodes):
        first = _add(client).get_json()
        nodes("http://other:9494").info_answer = _info(uid="uid-2")
        second = _add(client, name="other", url="http://other:9494").get_json()

        resp = client.put(f"/api/nodes/{second['id']}", json={"url": "http://nuc:9494/", "key": "node-key"})

        assert resp.status_code == 409
        assert (resp.get_json()["node_id"], resp.get_json()["name"]) == (first["id"], "nuc")
        assert _sm().nodes[1]["url"] == "http://other:9494"

    def test_an_address_where_an_added_node_answers_is_refused(self, client, nodes):
        first = _add(client).get_json()
        nodes("http://other:9494").info_answer = _info(uid="uid-2")
        second = _add(client, name="other", url="http://other:9494").get_json()
        nodes("http://nuc.lan:9494").info_answer = _info(uid="uid-1")  # the first node, by another name

        resp = client.put(f"/api/nodes/{second['id']}", json={"url": "http://nuc.lan:9494", "key": "node-key"})

        assert resp.status_code == 409 and resp.get_json()["node_id"] == first["id"]
        assert _sm().nodes[1]["url"] == "http://other:9494"

    def test_adding_a_second_node_at_a_saved_address_is_refused_even_while_it_is_offline(self, client, nodes):
        nodes("http://nuc:9494").info_answer = NodeUnreachableError("down")
        first = _add(client).get_json()
        resp = _add(client, name="again")
        assert resp.status_code == 409 and resp.get_json()["node_id"] == first["id"]


class TestRemovedDuringAGroupSave:
    def test_a_node_removed_while_a_group_save_checks_it_is_never_left_referenced(self, client, nodes, monkeypatch):
        """The group save checks its members against the nodes list under the settings lock, so a Remove that comes
        in meanwhile waits, sees the new member and is refused."""
        import threading

        from media_preview_generator import worker_groups as wg

        node_id = _add(client).get_json()["id"]
        real_validate = wg.validate_worker_groups
        checking, deleted = threading.Event(), []

        def slow_validate(value, **kwargs):
            clean = real_validate(value, **kwargs)
            checking.set()
            time.sleep(0.3)  # the other request's Remove arrives now
            return clean

        def remove():
            checking.wait(5)
            deleted.append(client.delete(f"/api/nodes/{node_id}").status_code)

        monkeypatch.setattr(wg, "validate_worker_groups", slow_validate)
        remover = threading.Thread(target=remove)
        remover.start()
        member = {"id": "m1", "resource": "cpu", "device": None, "count": 1, "job_types": ["previews"], "node": node_id}
        group = {"id": "g1", "name": "NUC", "enabled": True, "availability": {"mode": "always", "windows": []},
                 "members": [member]}  # fmt: skip
        _sm().update_worker_groups([group])
        remover.join(5)

        assert deleted == [409]
        assert [n["id"] for n in _sm().nodes] == [node_id]


class TestTheReference:
    """docs/reference.md is what a script follows: it must describe the routes and the stored shape as they are."""

    REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "reference.md"

    def test_every_setting_a_node_reads_is_in_the_reference(self):
        # A user moving the node's temp folder (named by its own Setup Health) must find NODE_TEMP_DIR somewhere.
        server = Path(api_nodes.__file__).resolve().parents[2] / "nodes" / "server"
        read = {"MODE", "FILE__"}
        for module in ("app.py", "identity.py", "wsgi.py", "tasks.py"):
            source = (server / module).read_text(encoding="utf-8")
            read |= set(re.findall(r'environ(?:\.get\(|\[)"([A-Z][A-Z0-9_]*)"', source))
        read.discard("NODE_IMAGE_ROLES")  # set by the Plex database image itself, never by a user
        text = self.REFERENCE.read_text(encoding="utf-8")
        assert {"NODE_TEMP_DIR", "NODE_HOST", "NODE_NAME"} <= read
        assert sorted(name for name in read if f"`{name}" not in text) == []

    def test_the_worker_groups_snapshot_documents_its_nodes(self):
        text = self.REFERENCE.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if "| GET | `/api/worker-groups` |" in line)
        assert "`nodes`" in row

    def test_the_workers_snapshot_documents_which_node_a_worker_runs_on(self):
        text = self.REFERENCE.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if "| GET | `/api/jobs/workers` |" in line)
        assert "`node`" in row and "`node_state`" in row

    def test_every_field_of_a_node_row_is_in_the_reference(self, client, nodes):
        node_id = _add(client).get_json()["id"]
        text = self.REFERENCE.read_text(encoding="utf-8")
        paragraph = text.split("A `NODE_ROW` is", 1)[1].split("\n\n", 1)[0]
        assert sorted(key for key in _row(client, node_id) if f"`{key}`" not in paragraph) == []

    def test_every_nodes_route_the_app_serves_is_in_the_reference(self, client):
        text = self.REFERENCE.read_text(encoding="utf-8")
        missing = []
        for rule in client.application.url_map.iter_rules():
            if rule.rule.startswith("/api/nodes"):
                path = rule.rule.replace("<node_id>", "{id}")
                methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
                missing += [f"{m} {path}" for m in methods if f"| {m} | `{path}` |" not in text]
        assert missing == []

    def test_a_plex_servers_database_node_choice_is_documented_as_it_is_stored(self):
        from media_preview_generator.markers.settings import default_plex_agent

        text = self.REFERENCE.read_text(encoding="utf-8")
        assert default_plex_agent() == {"enabled": False, "node_id": None}
        assert '"agent": {"enabled": false, "node_id": null}' in text and "`plex.agent.node_id`" in text
        assert "`plex.agent.url`" not in text and "`plex.agent.token`" not in text


class TestTheGuide:
    """The docs a user reads to understand a node card, and the pages that offer another machine's GPU."""

    DOCS = Path(__file__).resolve().parents[2] / "docs"

    def test_it_names_every_state_a_node_card_shows(self):
        # Every page takes a node state's name from app.js's one map.
        js = (Path(api_nodes.__file__).resolve().parents[1] / "static" / "js" / "app.js").read_text(encoding="utf-8")
        block = js.split("window.NODE_STATES = Object.freeze({", 1)[1].split("});", 1)[0]
        labels = re.findall(r"\w+: \{ tone: '\w+', label: '([^']+)'", block)
        guide = (self.DOCS / "guides.md").read_text(encoding="utf-8")
        section = guide.split("## Remote nodes", 1)[1].split("\n## ", 1)[0]
        assert {"Temp folder problem", "Added twice", "Offline"} <= set(labels)
        assert [label for label in labels if f"**{label}**" not in section] == []

    @pytest.mark.parametrize(
        "page", ["multi-server.md", "faq.md", "jellyfin-trickplay-gpu.md", "plex-preview-thumbnails-slow.md"]
    )
    def test_every_page_offering_another_machine_links_remote_nodes(self, page):
        assert "guides.md#remote-nodes" in (self.DOCS / page).read_text(encoding="utf-8")
