"""A real HTTP round trip: the app's NodeClient against create_node_app served by werkzeug on 127.0.0.1."""

from __future__ import annotations

import errno
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
from media_preview_generator.nodes.executor import (
    REQUEUE_NODE_NOT_STARTED,
    NodeUnavailableError,
    Placement,
    placed_on,
    requeue_for,
    run_on_node,
)
from media_preview_generator.nodes.protocol import PathMapping
from media_preview_generator.nodes.registry import set_registry
from media_preview_generator.nodes.server import app as node_app
from media_preview_generator.nodes.server import info

from .executor_fakes import NODE_ID, identity_registry, write_file

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


def _fills_temp_op(ctx, node_path, params):
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), os.path.join(ctx.temp_dir, "frames.tar"))


# When the watched op began and stopped (cleared by the test that uses it).
WATCHED = {"started": threading.Event(), "stopped": threading.Event()}


def _watched_op(ctx, node_path, params):
    WATCHED["started"].set()
    try:
        return _slow_op(ctx, node_path, params)
    finally:
        WATCHED["stopped"].set()


@pytest.fixture
def node(tmp_path, monkeypatch):
    for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_ROLES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NODE_NAME", "test-node")
    monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-tmp"))
    monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"})
    app = node_app.create_node_app(
        config_dir=str(tmp_path / "config"), roles=["worker"], key=KEY,
        ops={protocol.OP_PREVIEW_FRAMES: _frames_op, "test.slow": _slow_op, "test.watched": _watched_op,
             "test.fills_temp": _fills_temp_op},
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
                                  "build": health["node"]["build"], "protocols": [1], "roles": ["worker"]}  # fmt: skip
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

    def test_the_right_key_from_an_address_a_stale_client_shares_is_a_wait_not_a_refused_key(self, node):
        from media_preview_generator.nodes.client import NodeRateLimitedError, NodeUnreachableError
        from media_preview_generator.nodes.server import identity

        stale = _client(node[0], "an-old-key")
        for _ in range(identity.WRONG_KEY_BURST):
            with pytest.raises(NodeAuthError):
                stale.info()

        with pytest.raises(NodeRateLimitedError) as raised:
            _client(node[0]).info()  # this app, behind the same address (NAT, a proxy)
        assert isinstance(raised.value, NodeUnreachableError) and not isinstance(raised.value, NodeAuthError)
        assert "too many wrong keys" in str(raised.value)

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


class TestAppStop:
    def test_a_stopping_app_stops_the_task_it_runs_on_the_node_at_once(self, node, media):
        from media_preview_generator.nodes import executor
        from media_preview_generator.nodes.registry import set_registry

        from .executor_fakes import NODE_ID, NODE_NAME, identity_registry

        for event in WATCHED.values():
            event.clear()
        set_registry(identity_registry(_client(node[0])))
        ended: list[BaseException] = []

        def work():
            try:
                with executor.placed_on(executor.Placement(NODE_ID, NODE_NAME)):
                    executor.run_on_node("test.watched", str(media / "a.mkv"), {}, gpu=None, cancel_check=None)
            except BaseException as exc:  # noqa: BLE001 - the test reads how the worker's file ended
                ended.append(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            assert WATCHED["started"].wait(5)

            # The op starts on the node a moment before the node's answer reaches the worker.
            deadline = time.monotonic() + 5
            while True:
                began = time.monotonic()
                freed = executor.cancel_running_tasks()
                took = time.monotonic() - began
                if freed or time.monotonic() > deadline:
                    break
                time.sleep(0.01)
            assert freed == 1

            # Left alone it runs on to its node's unpolled-task expiry (10 s for this op).
            assert WATCHED["stopped"].wait(2)
            assert took <= executor.STOP_DELETE_BUDGET_S + 0.5
            worker.join(10)
            # Not the file's failure: the job stays running and the next start revives it.
            assert [type(exc) for exc in ended] == [executor.NodeUnavailableError]
            assert executor.cancel_running_tasks() == 0
        finally:
            set_registry(None)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a 0500 folder")
class TestATempFolderTheNodeCantWrite:
    """A read-only mount (or a disk remounted read-only) under NODE_TEMP_DIR: the node refuses every start."""

    @pytest.fixture
    def read_only_temp(self, node, tmp_path):
        temp_root = tmp_path / "node-tmp"
        temp_root.chmod(0o500)
        yield str(temp_root)
        temp_root.chmod(0o700)

    def test_the_node_says_so_in_its_info(self, node, read_only_temp):
        problem = _client(node[0]).info()["worker"]["temp"]["problem"]

        assert problem == f"The node can't write to its temp folder {read_only_temp} (Permission denied)"

    def test_a_writable_temp_folder_has_no_problem(self, node):
        assert _client(node[0]).info()["worker"]["temp"]["problem"] is None

    def test_a_file_sent_there_goes_back_in_the_queue_and_the_node_is_checked(self, node, read_only_temp):
        # Not a failed file: the node stays "ready" until checked, so each file sent to it would fail in turn.
        media = write_file(os.path.join(os.path.dirname(read_only_temp), "data", "Movie.mkv"), b"0123456789")
        registry = identity_registry(_client(node[0]))
        set_registry(registry)
        try:
            with placed_on(Placement(NODE_ID, "NAS")), pytest.raises(NodeUnavailableError) as raised:
                run_on_node(protocol.OP_PREVIEW_FRAMES, media, {"frames": 1}, gpu=None, cancel_check=None)
        finally:
            set_registry(None)

        assert raised.value.started is False and f"temp folder {read_only_temp}" in str(raised.value)
        assert requeue_for(raised.value).reason == REQUEUE_NODE_NOT_STARTED
        assert registry.reports == [("unavailable", NODE_ID)]


class TestATempFolderThatFillsMidTask:
    """A big file fills the node's temp folder: not the file's failure, and the next big file mustn't fail too."""

    def test_the_file_goes_back_in_the_queue_with_no_drop_and_the_node_is_checked(self, node, tmp_path):
        media = write_file(str(tmp_path / "data" / "Movie.mkv"), b"0123456789")
        registry = identity_registry(_client(node[0]))
        set_registry(registry)
        try:
            with placed_on(Placement(NODE_ID, "NAS")), pytest.raises(NodeUnavailableError) as raised:
                run_on_node("test.fills_temp", media, {}, gpu=None, cancel_check=None)
        finally:
            set_registry(None)

        assert raised.value.started is False and "temp folder" in str(raised.value) and "is full" in str(raised.value)
        assert requeue_for(raised.value).reason == REQUEUE_NODE_NOT_STARTED
        assert registry.reports == [("unavailable", NODE_ID)]


class TestANodeThatIsStopping:
    """SIGTERM: the node cancels its tasks and waits for their FFmpeg, and keeps answering meanwhile."""

    def test_it_says_so_and_a_file_sent_there_goes_elsewhere_without_a_drop(self, node, tmp_path):
        # A file requeued during that wait was started there again, never cancelled, and failed as "dropped twice".
        url, app = node
        client = _client(url)
        assert client.info()["stopping"] is False and client.health()["stopping"] is False
        app.extensions[node_app.EXTENSION].close()  # what the stop does first
        media = write_file(str(tmp_path / "data" / "Movie.mkv"), b"0123456789")
        registry = identity_registry(client)
        set_registry(registry)
        try:
            with placed_on(Placement(NODE_ID, "NAS")), pytest.raises(NodeUnavailableError) as raised:
                run_on_node(protocol.OP_PREVIEW_FRAMES, media, {"frames": 1}, gpu=None, cancel_check=None)
        finally:
            set_registry(None)

        assert raised.value.started is False and "is restarting" in str(raised.value)
        assert requeue_for(raised.value).reason == REQUEUE_NODE_NOT_STARTED
        assert registry.reports == [("unavailable", NODE_ID)]
        assert client.info()["stopping"] is True and client.health()["stopping"] is True
        assert app.extensions[node_app.EXTENSION].tasks.busy() == 0
