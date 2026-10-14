"""A real HTTP round trip: the app's NodeClient against create_node_app served by werkzeug on 127.0.0.1."""

from __future__ import annotations

import os
import threading
import time

import pytest
import requests
from werkzeug.serving import make_server

from media_preview_generator.nodes import protocol
from media_preview_generator.nodes.client import (
    NodeAddress,
    NodeAuthError,
    NodeClient,
    NodeProtocolError,
    NodeRequestError,
    NodeUnknownTaskError,
)
from media_preview_generator.nodes.protocol import PathMapping
from media_preview_generator.nodes.server import app as node_app
from media_preview_generator.nodes.server import info

KEY = "round-trip-key"


def _frames_op(ctx, node_path, params):
    ctx.progress(10.0, step="decode")
    ctx.log("info", f"reading {os.path.basename(node_path)}")
    path = os.path.join(ctx.temp_dir, "frames.tar")
    with open(path, "wb") as handle:
        handle.write(b"frames-tar-bytes")
    ctx.set_artifact(path)
    return {"frames": params.get("frames", 0)}


def _slow_op(ctx, node_path, params):
    deadline = time.monotonic() + 10
    while not ctx.cancel_check() and time.monotonic() < deadline:
        time.sleep(0.01)
    return {}


@pytest.fixture
def node(tmp_path, monkeypatch):
    for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_ROLES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NODE_NAME", "test-node")
    monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-tmp"))
    monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"})
    app = node_app.create_node_app(
        config_dir=str(tmp_path / "config"), roles=["worker"], key=KEY,
        ops={protocol.OP_PREVIEW_FRAMES: _frames_op, "test.slow": _slow_op},
    )  # fmt: skip
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", app
    server.shutdown()
    thread.join(5)
    app.extensions[node_app.EXTENSION].close()


def _client(url: str, key: str = KEY) -> NodeClient:
    session = requests.Session()
    session.trust_env = False  # a proxy from the environment must not sit between the test and 127.0.0.1
    return NodeClient(NodeAddress(url, key), session=session)


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x" * 10)
    return root


def _spec(media, op: str = protocol.OP_PREVIEW_FRAMES) -> dict:
    st = os.stat(media / "a.mkv")
    return {"op": op, "path": "/data/a.mkv", "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "mapping": [{"app_prefix": "/data", "node_prefix": str(media)}], "gpu": None, "params": {"frames": 3}}  # fmt: skip


def _wait(client: NodeClient, task_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = client.poll_task(task_id)
        if state["status"] in protocol.FINISHED_STATUSES:
            return state
        time.sleep(0.02)
    raise AssertionError("task never finished")


class TestRoundTrip:
    def test_health_and_info(self, node):
        url, app = node
        client = _client(url)

        health = client.health()
        details = client.info()

        node_id = app.extensions[node_app.EXTENSION].identity.id
        assert health["node"] == {"id": node_id, "name": "test-node", "version": health["node"]["version"],
                                  "protocols": [1], "roles": ["worker"]}  # fmt: skip
        assert details["node"]["id"] == node_id
        assert details["worker"]["gpus"] == [] and details["worker"]["busy"] == 0
        assert client.rescan_gpus()["worker"]["gpus"] == []

    def test_access_check(self, node, media):
        client = _client(node[0])
        st = os.stat(media / "a.mkv")

        answer = client.access_check(
            [PathMapping("/data", str(media))],
            [{"path": "/data/a.mkv", "size": st.st_size, "mtime_ns": st.st_mtime_ns},
             {"path": "/data/missing.mkv", "size": None, "mtime_ns": None}],
        )  # fmt: skip

        assert [row["ok"] for row in answer["results"]] == [True, False]
        assert answer["results"][1]["kind"] == protocol.NOT_VISIBLE_MISSING
        assert answer["nearby"] == {str(media): ["a.mkv"]}

    def test_start_poll_artifact_delete(self, node, media):
        client = _client(node[0])

        task_id = client.start_task(_spec(media))
        state = _wait(client, task_id)
        artifact = client.fetch_artifact(task_id)
        page = client.poll_task(task_id, log_from=state["next_log"])
        client.delete_task(task_id)

        assert state["status"] == protocol.STATUS_DONE and state["result"] == {"frames": 3}
        assert state["progress"] == {"percent": 10.0, "step": "decode"}
        assert any(line["message"] == "reading a.mkv" for line in state["log"])
        assert page["log"] == []
        assert artifact == b"frames-tar-bytes"
        with pytest.raises(NodeUnknownTaskError):
            client.poll_task(task_id)

    def test_cancel(self, node, media):
        client = _client(node[0])
        task_id = client.start_task(_spec(media, "test.slow"))

        client.cancel_task(task_id)

        assert _wait(client, task_id)["status"] == protocol.STATUS_CANCELLED

    def test_not_visible_comes_back_as_a_status(self, node, media):
        client = _client(node[0])
        spec = {**_spec(media), "size": 999}

        state = _wait(client, client.start_task(spec))

        assert state["status"] == protocol.STATUS_FILE_NOT_VISIBLE
        assert state["error"]["reason"] == protocol.NOT_VISIBLE_DIFFERENT_FILE

    def test_wrong_key_is_node_auth_error(self, node):
        with pytest.raises(NodeAuthError):
            _client(node[0], "wrong").info()

    def test_unknown_task_is_node_unknown_task_error(self, node):
        client = _client(node[0])

        with pytest.raises(NodeUnknownTaskError):
            client.poll_task("nope")
        with pytest.raises(NodeUnknownTaskError):
            client.cancel_task("nope")
        with pytest.raises(NodeUnknownTaskError):
            client.delete_task("nope")

    def test_unknown_op_is_node_request_error(self, node, media):
        with pytest.raises(NodeRequestError):
            _client(node[0]).start_task(_spec(media, "no.such.op"))

    def test_missing_artifact_is_a_protocol_error(self, node, media):
        client = _client(node[0])
        task_id = client.start_task(_spec(media, "test.slow"))
        client.cancel_task(task_id)
        _wait(client, task_id)

        with pytest.raises(NodeProtocolError):
            client.fetch_artifact(task_id)

    def test_log(self, node):
        lines = _client(node[0]).log(50)["lines"]

        assert any("ready" in line["message"] for line in lines)
