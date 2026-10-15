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
from media_preview_generator.nodes.registry import NodeRegistry, set_registry
from media_preview_generator.nodes.server import app as node_app
from media_preview_generator.nodes.server import info, tasks

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
            # Not queued again for another node, where nothing would free it, and not the file's failure (the app is
            # stopping): the job stays running and the next start revives it.
            assert [type(exc) for exc in ended] == [executor.NodeTaskCancelledError]
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

    def test_the_node_reads_full_after_it_so_the_app_holds_it(self, node, tmp_path, monkeypatch):
        # The check the app runs at once after the file came back found an empty folder that works: the node read
        # ready and was sent the next big file, five times over with only node workers, until the file failed as
        # "couldn't start the file 5 times". The app holds a node whose info names a temp problem (test_registry).
        # Free space held still: other tests share this disk.
        monkeypatch.setattr(tasks, "_free_bytes", lambda path: 4 << 20)
        media = write_file(str(tmp_path / "data" / "Movie.mkv"), b"0123456789")
        client = _client(node[0])
        set_registry(identity_registry(client))
        try:
            with placed_on(Placement(NODE_ID, "NAS")), pytest.raises(NodeUnavailableError):
                run_on_node("test.fills_temp", media, {}, gpu=None, cancel_check=None)
        finally:
            set_registry(None)

        temp = client.info()["worker"]["temp"]
        assert temp["problem"] == f"The node's temp folder {temp['path']} filled up while working on a file (4 MB free)"

    def test_ffmpeg_filling_it_sends_no_frames_and_the_file_goes_back_in_the_queue(self, node, tmp_path, monkeypatch):
        # The node's real preview op: FFmpeg stopped with "No space left on device" (exit 228) after 2 of the 44
        # thumbnails, and those 2 went to Plex as the episode's preview, marked fresh so later scans skipped it.
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from media_preview_generator.nodes import remote_ops
        from media_preview_generator.nodes.server import ops
        from media_preview_generator.processing import ffmpeg_runner, generator

        full = "[out#0/image2 @ 0x1] Task finished with error code: -28 (No space left on device)"

        def full_folder_runner(*, output_folder, **_kwargs):
            def run(*_args, **_run_kwargs):
                for number in (1, 2):
                    write_file(os.path.join(output_folder, f"img-{number:06d}.jpg"), b"\xff\xd8\xff\xdb")
                return 228, 1.0, "10x", [full]

            return run

        url, app = node
        monkeypatch.setitem(
            app.extensions[node_app.EXTENSION].tasks._ops, protocol.OP_PREVIEW_FRAMES, ops.preview_frames
        )
        monkeypatch.setattr(ffmpeg_runner, "create_ffmpeg_runner", full_folder_runner)
        monkeypatch.setattr(generator, "_probe_max_keyframe_gap", lambda *_a, **_k: None)
        monkeypatch.setattr(generator, "MediaInfo", MagicMock(**{"parse.return_value": MagicMock(video_tracks=[])}))
        monkeypatch.setattr(tasks, "_free_bytes", lambda path: 4 << 20)
        media = write_file(str(tmp_path / "data" / "Movie.mkv"), b"0123456789")
        frames = tmp_path / "app-frames"
        frames.mkdir()
        config = SimpleNamespace(plex_bif_frame_interval=10, tonemap_algorithm="hable", thumbnail_quality=4,
                                 ffmpeg_threads=2, log_level="INFO", server_display_name=None)  # fmt: skip
        client = _client(url)
        set_registry(identity_registry(client))
        try:
            with placed_on(Placement(NODE_ID, "NAS")), pytest.raises(NodeUnavailableError) as raised:
                remote_ops.generate_images(media, str(frames), None, None, config)
        finally:
            set_registry(None)

        assert raised.value.started is False and "is full (No space left on device)" in str(raised.value)
        assert requeue_for(raised.value).reason == REQUEUE_NODE_NOT_STARTED
        assert list(frames.iterdir()) == []
        assert "filled up while working on a file" in client.info()["worker"]["temp"]["problem"]


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

    def test_the_file_it_was_running_goes_back_in_the_queue_without_a_drop(self, node, tmp_path):
        # docker restart twice while a file ran there: the stop emptied the task table, the app's next poll found the
        # task unknown and counted a drop each time, and the file failed "it dropped the file twice".
        url, app = node
        for event in WATCHED.values():
            event.clear()
        media = write_file(str(tmp_path / "data" / "Movie.mkv"), b"0123456789")
        registry = identity_registry(_client(url))
        set_registry(registry)
        ended: list[BaseException] = []

        def work() -> None:
            try:
                with placed_on(Placement(NODE_ID, "NAS")):
                    run_on_node("test.watched", media, {}, gpu=None, cancel_check=None)
            except BaseException as exc:  # noqa: BLE001 - the test reads how the worker's file ended
                ended.append(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            assert WATCHED["started"].wait(5)
            table = app.extensions[node_app.EXTENSION].tasks
            table.close(wait_s=5)  # what the node's SIGTERM does first
            table.wait_until_told(timeout_s=5)  # still serving, as the node does before it exits
            worker.join(10)
        finally:
            set_registry(None)

        assert [type(exc) for exc in ended] == [NodeUnavailableError]
        assert ended[0].started is False and str(ended[0]) == "NAS is restarting"
        assert requeue_for(ended[0]).reason == REQUEUE_NODE_NOT_STARTED
        assert registry.reports == [("unavailable", NODE_ID)]


class TestAPlexDatabaseNodeSwappedAtItsAddress:
    """Another machine, given the same key, starts answering at a Plex database node's address between the app's
    check of the node and its write: that machine writes nothing into its Plex, and the app checks the node again."""

    @pytest.fixture
    def swapped(self, tmp_path, monkeypatch):
        from media_preview_generator.markers.publishers import plex_db
        from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager
        from tests.markers.test_plex_db_publisher import _make_db

        for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_ROLES"):
            monkeypatch.delenv(name, raising=False)
        # Stands in for Plex holding each database open (the role's lock proof).
        monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)
        machines = {}
        for name in ("a", "b"):
            folder = tmp_path / f"plex-{name}"
            _make_db(folder, journal_mode="wal")
            monkeypatch.setenv("PLEX_CONFIG_DIR", str(folder))
            app = node_app.create_node_app(config_dir=str(tmp_path / f"node-{name}"), roles=["plex_db"], key=KEY)
            machines[name] = (app, folder)
        answering = {"app": machines["a"][0]}
        server = make_server("127.0.0.1", 0, lambda env, start: answering["app"](env, start), threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"

        reset_settings_manager()
        uid_a = machines["a"][0].extensions[node_app.EXTENSION].identity.id
        get_settings_manager(str(tmp_path / "app")).set(
            "nodes",
            [{"id": "n1", "node_uid": uid_a, "name": "Plex box", "url": url, "key": KEY, "enabled": True,
              "path_mappings": [], "added_at": "2026-10-15T00:00:00+00:00"}],
        )  # fmt: skip
        registry = NodeRegistry(
            client_factory=lambda record: _client(record["url"], record["key"]),
            # The server's Plex machine id isn't known: nothing ties a new id to it.
            served_plex_ids=lambda node_id: [""],
            attempt_gap_s=0.0,
        )
        set_registry(registry)
        yield answering, machines
        set_registry(None)
        reset_settings_manager()
        server.shutdown()
        thread.join(5)
        for app, _folder in machines.values():
            app.extensions[node_app.EXTENSION].close()

    def test_the_other_machine_writes_nothing_and_the_app_holds_the_node_until_it_is_confirmed(self, swapped):
        from media_preview_generator.markers.publishers.plex_remote import (
            AgentUnreachableError,
            plex_database,
            write_request_from_json,
        )
        from media_preview_generator.markers.settings import load_server
        from media_preview_generator.nodes.registry import get_registry

        from .test_plex_db_role import INTRO, _served, _write_body

        answering, machines = swapped
        settings = load_server(
            {"enabled": True, "plex": {"db_write_confirmed_at": "2026-01-01", "agent": {"enabled": True,
             "node_id": "n1"}}},
            "plex",
        )  # fmt: skip
        session = requests.Session()
        session.trust_env = False
        db = plex_database(settings, path_provider=lambda: "", label="Home", session=session)
        write = write_request_from_json(_write_body(rating_key=7, wanted=[INTRO])["write"])

        assert get_registry().plex_db_hold("n1") is None  # a check heard the saved id
        db.write_item(write, deadline=time.monotonic() + 10)
        assert _served(machines["a"][1]) == [("intro", 11_000, 37_000)]

        answering["app"] = machines["b"][0]  # the same key, another machine
        with pytest.raises(AgentUnreachableError, match="isn't the Plex database node saved there"):
            db.write_item(write, deadline=time.monotonic() + 10)

        assert _served(machines["b"][1]) == []
        assert get_registry().plex_db_hold("n1") is not None  # its work waits while it is checked again, at once
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "reports a new id" not in get_registry().plex_db_refusal("n1"):
            time.sleep(0.02)
        # It has another id, and nothing ties it to the server's Plex: held until its key is typed again.
        assert "reports a new id" in get_registry().plex_db_refusal("n1")
