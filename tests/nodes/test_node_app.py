"""The node's Flask app: auth, the protocol header, the node routes, roles and the Plex database hook (spec §3)."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import time
import types
from datetime import datetime

import pytest

import media_preview_generator.nodes.server as server_package
from media_preview_generator import __version__
from media_preview_generator.nodes import protocol
from media_preview_generator.nodes.server import app as node_app
from media_preview_generator.nodes.server import info

KEY = "test-key-123"
HEADERS = {"Authorization": f"Bearer {KEY}", protocol.PROTOCOL_HEADER: "1"}


@pytest.fixture(autouse=True)
def _node_env(tmp_path, monkeypatch):
    for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_NAME", "NODE_ROLES", "PLEX_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-tmp"))
    monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"})


@pytest.fixture
def make_app():
    made = []

    def make(**kwargs):
        kwargs.setdefault("key", KEY)
        kwargs.setdefault("ops", {})
        app = node_app.create_node_app(**kwargs)
        made.append(app)
        return app

    yield make
    for app in made:
        app.extensions[node_app.EXTENSION].close()


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x" * 10)
    return root


def _wait_done(client, task_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = client.get(f"/v1/tasks/{task_id}", headers=HEADERS).get_json()
        if body["status"] != protocol.STATUS_RUNNING:
            return body
        time.sleep(0.01)
    raise AssertionError("task never finished")


class TestAuth:
    def test_health_needs_no_key(self, make_app):
        response = make_app().test_client().get("/v1/health")

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["node"]["version"] == __version__
        assert body["node"]["protocols"] == [1] and body["node"]["roles"] == ["worker"]
        assert "agent" not in body

    @pytest.mark.parametrize(
        "headers",
        [
            {protocol.PROTOCOL_HEADER: "1"},
            {"Authorization": "Bearer wrong", protocol.PROTOCOL_HEADER: "1"},
            {"Authorization": KEY, protocol.PROTOCOL_HEADER: "1"},
            {"Authorization": "Bearer ", protocol.PROTOCOL_HEADER: "1"},
        ],
    )
    def test_missing_or_wrong_key_is_401(self, make_app, headers):
        response = make_app().test_client().get("/v1/info", headers=headers)

        assert response.status_code == 401
        assert response.get_json()["error"] == {"kind": protocol.KIND_AUTH}

    def test_non_ascii_key_is_401_not_500(self, make_app):
        headers = {"Authorization": "Bearer clé".encode().decode("latin-1"), protocol.PROTOCOL_HEADER: "1"}

        assert make_app().test_client().get("/v1/info", headers=headers).status_code == 401

    @pytest.mark.parametrize("route", ["/v1/info", "/v1/log", "/v1/tasks/x", "/v1/tasks/x/artifact"])
    def test_every_other_get_route_needs_the_key(self, make_app, route):
        assert make_app().test_client().get(route).status_code == 401

    @pytest.mark.parametrize(
        "route",
        ["/v1/gpus/rescan", "/v1/access-check", "/v1/tasks", "/v1/tasks/x/cancel", "/v1/tasks/x/pause",
         "/v1/tasks/x/resume"],
    )  # fmt: skip
    def test_every_post_route_needs_the_key(self, make_app, route):
        assert make_app().test_client().post(route, json={}).status_code == 401

    def test_delete_needs_the_key(self, make_app):
        assert make_app().test_client().delete("/v1/tasks/x").status_code == 401


class TestProtocolHeader:
    @pytest.mark.parametrize("value", [None, "2", "one", ""])
    def test_missing_or_other_protocol_is_409(self, make_app, value):
        headers = {"Authorization": f"Bearer {KEY}"}
        if value is not None:
            headers[protocol.PROTOCOL_HEADER] = value

        response = make_app().test_client().get("/v1/info", headers=headers)

        assert response.status_code == 409
        assert response.get_json()["error"]["kind"] == protocol.KIND_PROTOCOL

    def test_auth_is_checked_before_protocol(self, make_app):
        assert make_app().test_client().get("/v1/info").status_code == 401


class TestInfo:
    def test_worker_block_is_json_safe_with_detection_mocked(self, make_app, monkeypatch):
        import media_preview_generator.gpu.detect as gpu_detect

        monkeypatch.setattr(
            gpu_detect,
            "detect_all_gpus",
            lambda: [
                ("NVIDIA", "cuda:0", {"name": "RTX 4090", "status": "ok", "vram": object()}),
                ("INTEL", "/dev/dri/renderD129", {"name": "Arc", "status": "failed", "error": OSError("no")}),
                ("APPLE", "videotoolbox", {"name": "M2"}),
            ],
        )

        response = make_app().test_client().get("/v1/info", headers=HEADERS)

        body = response.get_json()
        json.dumps(body)
        assert body["platform"]["python"] and body["platform"]["os"] and "arch" in body["platform"]
        worker = body["worker"]
        assert worker["gpus"] == [
            {"type": "NVIDIA", "device": "cuda:0", "name": "RTX 4090", "status": "ok", "error": None},
            {"type": "INTEL", "device": "/dev/dri/renderD129", "name": "Arc", "status": "failed", "error": "no"},
            {"type": "APPLE", "device": "videotoolbox", "name": "M2", "status": "ok", "error": None},
        ]
        assert worker["cpu"]["cores"] >= 1
        assert set(worker["cpu"]) == {"cores", "load1", "ram_total", "ram_available"}
        assert worker["temp"]["path"] == os.environ["NODE_TEMP_DIR"] and worker["temp"]["free_bytes"] > 0
        assert worker["busy"] == 0
        assert worker["ffmpeg"] == {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"}
        assert worker["gpu_checked_at"] is not None and worker["gpu_error"] is None
        assert "plex_db" not in body

    def test_info_still_answers_when_gpu_detection_raises(self, make_app, monkeypatch):
        import media_preview_generator.gpu.detect as gpu_detect

        def detect():
            raise OSError("vulkaninfo: device lost")

        monkeypatch.setattr(gpu_detect, "detect_all_gpus", detect)

        response = make_app().test_client().get("/v1/info", headers=HEADERS)

        assert response.status_code == 200
        worker = response.get_json()["worker"]
        assert worker["gpus"] == [] and worker["gpu_error"] == "OSError: vulkaninfo: device lost"
        assert worker["cpu"]["cores"] >= 1

    def test_rescan_runs_detection_again(self, make_app, monkeypatch):
        import media_preview_generator.gpu.detect as gpu_detect

        found = [[]]
        monkeypatch.setattr(gpu_detect, "detect_all_gpus", lambda: found[0])
        client = make_app().test_client()
        client.get("/v1/info", headers=HEADERS)
        found[0] = [("NVIDIA", "cuda:0", {"name": "RTX"})]

        body = client.post("/v1/gpus/rescan", headers=HEADERS).get_json()

        assert [g["device"] for g in body["worker"]["gpus"]] == ["cuda:0"]
        assert body["worker"]["gpu_checked_at"] is not None


class TestAccessCheck:
    def test_results_and_nearby(self, make_app, media):
        st = os.stat(media / "a.mkv")
        body = {
            "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}],
            "files": [
                {"path": "/data/a.mkv", "size": st.st_size, "mtime_ns": st.st_mtime_ns},
                {"path": "/data/b.mkv", "size": None, "mtime_ns": None},
                {"path": "/other/c.mkv"},
            ],
        }

        response = make_app().test_client().post("/v1/access-check", headers=HEADERS, json=body)

        answer = response.get_json()
        assert response.status_code == 200
        ok, missing, unmapped = answer["results"]
        assert ok == {"path": "/data/a.mkv", "ok": True, "node_path": os.path.realpath(media / "a.mkv"),
                      "kind": None, "message": ""}  # fmt: skip
        assert (missing["ok"], missing["kind"], missing["node_path"]) == (False, "missing", str(media / "b.mkv"))
        assert (unmapped["ok"], unmapped["kind"], unmapped["node_path"]) == (False, "unmapped", None)
        assert answer["nearby"] == {str(media): ["a.mkv"]}

    @pytest.mark.parametrize(
        "body",
        [
            {"mapping": "x", "files": []},
            {"mapping": [], "files": "x"},
            {"mapping": [], "files": [{"size": 1}]},
            {"mapping": [], "files": [{"path": "/a", "size": "big"}]},
            {"mapping": [], "files": [{"path": "/a"}] * 201},
        ],
    )
    def test_malformed_body_is_400(self, make_app, body):
        response = make_app().test_client().post("/v1/access-check", headers=HEADERS, json=body)

        assert response.status_code == 400
        assert response.get_json()["error"]["kind"] == protocol.KIND_REQUEST

    def test_body_that_isnt_an_object_is_400(self, make_app):
        response = make_app().test_client().post("/v1/access-check", headers=HEADERS, json=[1])

        assert response.status_code == 400


class TestTaskRoutes:
    def test_start_poll_artifact_delete(self, make_app, media):
        def op(ctx, node_path, params):
            path = os.path.join(ctx.temp_dir, "frames.tar")
            with open(path, "wb") as handle:
                handle.write(b"TAR!")
            ctx.set_artifact(path)
            return {"frames": 1}

        client = make_app(ops={"test.op": op}).test_client()
        spec = {"op": "test.op", "path": "/data/a.mkv", "size": 10, "mtime_ns": None,
                "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}], "gpu": None, "params": {}}  # fmt: skip

        started = client.post("/v1/tasks", headers=HEADERS, json=spec)
        assert started.status_code == 202
        task_id = started.get_json()["id"]
        state = _wait_done(client, task_id)
        artifact = client.get(f"/v1/tasks/{task_id}/artifact", headers=HEADERS)
        deleted = client.delete(f"/v1/tasks/{task_id}", headers=HEADERS)
        after = client.get(f"/v1/tasks/{task_id}", headers=HEADERS)

        assert state["status"] == "done" and state["result"] == {"frames": 1} and state["node"]["roles"] == ["worker"]
        assert set(state) >= {"id", "op", "status", "progress", "log", "next_log", "result", "error", "seconds"}
        assert artifact.status_code == 200 and artifact.data == b"TAR!"
        assert deleted.status_code == 200 and deleted.get_json()["ok"] is True
        assert after.status_code == 404 and after.get_json()["error"]["kind"] == protocol.KIND_UNKNOWN_TASK

    def test_a_repeated_start_with_the_same_client_id_answers_the_same_task(self, make_app, media):
        runs = []
        client = make_app(ops={"test.op": lambda *a: runs.append(1) or {}}).test_client()
        client_id = "fedcba9876543210fedcba9876543210"
        spec = {"op": "test.op", "path": "/data/a.mkv", "client_task_id": client_id,
                "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}]}  # fmt: skip

        first = client.post("/v1/tasks", headers=HEADERS, json=spec)
        _wait_done(client, client_id)
        again = client.post("/v1/tasks", headers=HEADERS, json=spec)

        assert (first.status_code, first.get_json()["id"]) == (202, client_id)
        assert (again.status_code, again.get_json()["id"]) == (202, client_id)
        assert runs == [1]

    def test_a_start_after_its_delete_is_409_task_deleted(self, make_app, media):
        runs = []
        client = make_app(ops={"test.op": lambda *a: runs.append(1) or {}}).test_client()
        client_id = "fedcba9876543210fedcba9876543210"
        spec = {"op": "test.op", "path": "/data/a.mkv", "client_task_id": client_id,
                "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}]}  # fmt: skip

        deleted = client.delete(f"/v1/tasks/{client_id}", headers=HEADERS)
        late = client.post("/v1/tasks", headers=HEADERS, json=spec)

        assert deleted.status_code == 404
        assert late.status_code == 409 and late.get_json()["error"]["kind"] == protocol.KIND_TASK_DELETED
        assert runs == []

    def test_no_artifact_is_404_request(self, make_app, media):
        client = make_app(ops={"test.op": lambda *a: {}}).test_client()
        spec = {"op": "test.op", "path": "/data/a.mkv", "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}]}
        task_id = client.post("/v1/tasks", headers=HEADERS, json=spec).get_json()["id"]
        _wait_done(client, task_id)

        response = client.get(f"/v1/tasks/{task_id}/artifact", headers=HEADERS)

        assert response.status_code == 404 and response.get_json()["error"]["kind"] == protocol.KIND_REQUEST

    def test_a_full_temp_disk_is_400_with_the_cause_not_500(self, make_app, media, monkeypatch):
        import tempfile

        def full(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(tempfile, "mkdtemp", full)
        spec = {"op": "test.op", "path": "/data/a.mkv", "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}]}

        response = make_app(ops={"test.op": lambda *a: {}}).test_client().post("/v1/tasks", headers=HEADERS, json=spec)

        # Below 500: the app reads any 5xx as a node that didn't answer, and would bounce the file instead.
        assert response.status_code == 400
        error = response.get_json()["error"]
        assert error["kind"] == protocol.KIND_FAILED and "is full (No space left on device)" in error["message"]

    def test_unknown_op_is_400(self, make_app):
        response = make_app().test_client().post("/v1/tasks", headers=HEADERS, json={"op": "x", "path": "/a"})

        assert response.status_code == 400 and response.get_json()["error"]["kind"] == protocol.KIND_REQUEST

    @pytest.mark.parametrize(
        ("method", "route"),
        [("get", ""), ("post", "/cancel"), ("post", "/pause"), ("post", "/resume"), ("get", "/artifact"),
         ("delete", "")],
    )  # fmt: skip
    def test_unknown_task_is_404_unknown_task(self, make_app, method, route):
        client = make_app().test_client()

        response = getattr(client, method)(f"/v1/tasks/nope{route}", headers=HEADERS)

        assert response.status_code == 404
        assert response.get_json()["error"]["kind"] == protocol.KIND_UNKNOWN_TASK

    def test_bad_log_from_is_400(self, make_app):
        response = make_app().test_client().get("/v1/tasks/x?log_from=abc", headers=HEADERS)

        assert response.status_code == 400

    def test_body_over_2_mb_is_refused(self, make_app):
        response = (
            make_app()
            .test_client()
            .post("/v1/tasks", headers={**HEADERS, "Content-Type": "application/json"}, data=b" " * (3 * 1024 * 1024))
        )

        assert response.status_code == 413
        assert response.get_json()["error"]["kind"] == protocol.KIND_REQUEST


class TestLogRoute:
    def test_returns_the_last_lines(self, make_app):
        from loguru import logger

        client = make_app().test_client()
        before = time.time()
        for n in range(5):
            logger.info("node line {}", n)

        body = client.get("/v1/log?lines=2", headers=HEADERS).get_json()

        assert [line["message"] for line in body["lines"]] == ["node line 3", "node line 4"]
        line = body["lines"][0]
        assert set(line) == {"time", "ts", "level", "message"}
        # ``ts`` is the instant with no zone to misread; ``time`` keeps the node's offset, and both are the same moment.
        assert before - 1 <= line["ts"] <= time.time() + 1
        assert abs(datetime.fromisoformat(line["time"]).timestamp() - line["ts"]) < 1

    def test_bad_lines_is_400(self, make_app):
        assert make_app().test_client().get("/v1/log?lines=x", headers=HEADERS).status_code == 400


class TestRoles:
    def test_node_roles_env(self, make_app, monkeypatch):
        monkeypatch.setenv("NODE_ROLES", "worker, worker")

        body = make_app().test_client().get("/v1/health").get_json()

        assert body["node"]["roles"] == ["worker"]

    @pytest.mark.parametrize("value", ["gpu", "worker,plexdb"])
    def test_unknown_role_refuses_to_start(self, make_app, monkeypatch, value):
        monkeypatch.setenv("NODE_ROLES", value)

        with pytest.raises(SystemExit, match="unknown role"):
            make_app()

    def test_empty_roles_list_refuses_to_start(self, make_app):
        with pytest.raises(SystemExit):
            make_app(roles=[])

    def test_plex_db_without_plex_config_dir_refuses_to_start(self, make_app):
        with pytest.raises(SystemExit, match="PLEX_CONFIG_DIR"):
            make_app(roles=["plex_db"])

    def test_plex_db_role_registers_and_carries_the_agent_block(self, make_app, monkeypatch, tmp_path):
        calls = []
        fake = types.ModuleType("media_preview_generator.nodes.server.plex_db")
        fake.register_plex_db = lambda app, **kwargs: calls.append(kwargs)
        fake.agent_block = lambda: {"version": "1.1.2", "protocols": [1], "capabilities": []}
        fake.info_block = lambda: {"config_dir": "/plex", "db_present": True, "machine_identifier": "abc"}
        monkeypatch.setitem(sys.modules, fake.__name__, fake)
        # `from . import plex_db` reads the package attribute first once the real module has been imported.
        monkeypatch.setattr(server_package, "plex_db", fake, raising=False)
        monkeypatch.setenv("PLEX_CONFIG_DIR", "/plex")

        client = make_app(roles=["plex_db"]).test_client()
        health = client.get("/v1/health").get_json()
        body = client.get("/v1/info", headers=HEADERS).get_json()

        assert calls == [{"plex_config_dir": "/plex", "key": KEY}]
        assert health["agent"]["version"] == "1.1.2" and health["node"]["roles"] == ["plex_db"]
        assert body["plex_db"] == {"config_dir": "/plex", "db_present": True, "machine_identifier": "abc"}
        assert "worker" not in body

    def test_plex_db_only_node_has_no_worker_routes_to_run(self, make_app, monkeypatch):
        fake = types.ModuleType("media_preview_generator.nodes.server.plex_db")
        fake.register_plex_db = lambda app, **kwargs: None
        fake.agent_block = lambda: {}
        monkeypatch.setitem(sys.modules, fake.__name__, fake)
        # `from . import plex_db` reads the package attribute first once the real module has been imported.
        monkeypatch.setattr(server_package, "plex_db", fake, raising=False)
        monkeypatch.setenv("PLEX_CONFIG_DIR", "/plex")
        client = make_app(roles=["plex_db"]).test_client()

        assert client.post("/v1/gpus/rescan", headers=HEADERS).status_code == 400
        assert client.post("/v1/tasks", headers=HEADERS, json={"op": "preview.frames", "path": "/a"}).status_code == 400

    def test_plex_db_only_node_lists_no_folders(self, make_app, monkeypatch, tmp_path):
        fake = types.ModuleType("media_preview_generator.nodes.server.plex_db")
        fake.register_plex_db = lambda app, **kwargs: None
        fake.agent_block = lambda: {}
        monkeypatch.setitem(sys.modules, fake.__name__, fake)
        monkeypatch.setattr(server_package, "plex_db", fake, raising=False)
        monkeypatch.setenv("PLEX_CONFIG_DIR", "/plex")
        private = tmp_path / "Users" / "steve" / "Documents"
        private.mkdir(parents=True)
        (private / "passwords.kdbx").write_bytes(b"secret")
        body = {
            "mapping": [{"app_prefix": "/x", "node_prefix": str(tmp_path)}],
            "files": [{"path": "/x/Users/steve/Documents/nope/a.mkv"}],
        }

        response = make_app(roles=["plex_db"]).test_client().post("/v1/access-check", headers=HEADERS, json=body)

        assert response.status_code == 400
        answer = response.get_json()
        assert answer["error"]["kind"] == protocol.KIND_REQUEST and "nearby" not in answer
        assert "passwords" not in response.get_data(as_text=True)

    def test_plex_db_only_node_loads_no_worker_modules(self, tmp_path):
        script = f"""
import sys, types
fake = types.ModuleType("media_preview_generator.nodes.server.plex_db")
fake.register_plex_db = lambda app, **kwargs: None
fake.agent_block = lambda: {{}}
sys.modules[fake.__name__] = fake
from media_preview_generator.nodes.server.app import create_node_app
create_node_app(config_dir={str(tmp_path)!r}, roles=["plex_db"], key="k")
heavy = [m for m in ("numpy", "requests", "media_preview_generator.gpu.detect",
                     "media_preview_generator.nodes.server.info", "media_preview_generator.config") if m in sys.modules]
print(heavy)
"""
        env = {**os.environ, "PLEX_CONFIG_DIR": "/plex", "NODE_TEMP_DIR": str(tmp_path / "t")}

        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)

        assert done.returncode == 0, done.stderr
        assert done.stdout.strip().splitlines()[-1] == "[]"
