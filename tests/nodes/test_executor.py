"""nodes.executor: one op on a node for a remote worker -- start, poll, log lines, cancel, pause, errors."""

from __future__ import annotations

import io
import os
import tarfile
import threading

import pytest
from loguru import logger

from media_preview_generator.jobs.worker import is_job_thread_for, job_thread
from media_preview_generator.nodes import executor, protocol
from media_preview_generator.nodes.client import NodeAuthError, NodeUnknownTaskError
from media_preview_generator.nodes.executor import (
    FileNotVisibleError,
    NodeTaskCancelledError,
    NodeTaskError,
    NodeUnavailableError,
    Placement,
    current_placement,
    placed_on,
    run_on_node,
)
from media_preview_generator.nodes.registry import set_registry
from media_preview_generator.processing.generator import FALLBACK_HWACCEL, FALLBACK_STALL, CodecNotSupportedError

from .executor_fakes import NODE_ID, NODE_NAME, FakeRegistry, ScriptedClient, finished, running, unreachable

PLACEMENT = Placement(NODE_ID, NODE_NAME)


@pytest.fixture(autouse=True)
def _fast_polls(monkeypatch):
    monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.0)
    yield
    set_registry(None)


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "data" / "Show" / "S01E01.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0123456789")
    return str(path)


def _install(client, **kwargs) -> FakeRegistry:
    registry = FakeRegistry({NODE_ID: client}, **kwargs)
    set_registry(registry)
    return registry


def _run(media, **kwargs):
    kwargs.setdefault("gpu", None)
    kwargs.setdefault("cancel_check", None)
    with placed_on(PLACEMENT):
        return run_on_node(protocol.OP_INTRO_FINGERPRINT, media, {"duration_ms": 1000}, **kwargs)


def _tar(entries: list[tuple[str, bytes]], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buf.getvalue()


class TestPlacement:
    def test_placed_on_sets_and_restores_the_threads_placement(self):
        assert current_placement() is None
        with placed_on(PLACEMENT):
            assert current_placement() == PLACEMENT
            with placed_on(None):
                assert current_placement() is None
            assert current_placement() == PLACEMENT
        assert current_placement() is None

    def test_a_placement_belongs_to_its_own_thread(self):
        seen = []
        with placed_on(PLACEMENT):
            thread = threading.Thread(target=lambda: seen.append(current_placement()))
            thread.start()
            thread.join()
        assert seen == [None]

    def test_run_on_node_without_a_placement_is_a_bug(self, media):
        _install(ScriptedClient([finished(protocol.STATUS_DONE, result={})]))
        with pytest.raises(RuntimeError, match="placement"):
            run_on_node(protocol.OP_INTRO_FINGERPRINT, media, {}, gpu=None, cancel_check=None)


class TestStartAndResult:
    def test_spec_carries_the_apps_path_identity_mapping_gpu_and_params(self, media):
        client = ScriptedClient([running(), finished(protocol.STATUS_DONE, result={"points": 1})])
        _install(client)

        result = _run(media, gpu=("NVIDIA", "cuda:0"))

        assert result == {"points": 1}
        stat = os.stat(media)
        assert client.spec == {
            "op": protocol.OP_INTRO_FINGERPRINT,
            "path": media,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "mapping": [{"app_prefix": "/data", "node_prefix": "/mnt/media"}],
            "gpu": {"type": "NVIDIA", "device": "cuda:0"},
            "params": {"duration_ms": 1000},
        }
        assert client.named("delete") == [("delete",)]

    def test_cpu_task_sends_no_gpu(self, media):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        _install(client)
        _run(media)
        assert client.spec["gpu"] is None

    def test_missing_app_file_raises_oserror_before_the_node_is_asked(self, tmp_path):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        _install(client)
        with pytest.raises(FileNotFoundError):
            _run(str(tmp_path / "gone.mkv"))
        assert client.calls == []

    def test_a_removed_node_is_unavailable(self, media):
        set_registry(FakeRegistry({}))
        with pytest.raises(NodeUnavailableError, match="no longer configured"):
            _run(media)

    def test_log_from_follows_next_log(self, media):
        client = ScriptedClient(
            [running(next_log=3), running(next_log=5), finished(protocol.STATUS_DONE, result={}, next_log=5)]
        )
        _install(client)
        _run(media)
        assert [c[1] for c in client.named("poll")] == [0, 3, 5]


class TestArtifact:
    def test_frames_are_unpacked_into_the_folder(self, media, tmp_path):
        dest = tmp_path / "frames"
        dest.mkdir()
        tar = _tar([("0000000000.jpg", b"a"), ("0000000005.jpg", b"b")])
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={"frames": 2})], artifact=tar)
        _install(client)

        _run(media, artifact_to=str(dest))

        assert sorted(os.listdir(dest)) == ["0000000000.jpg", "0000000005.jpg"]
        assert (dest / "0000000005.jpg").read_bytes() == b"b"
        assert client.named("artifact") and client.named("delete")

    @pytest.mark.parametrize(
        ("entries", "symlink"),
        [
            ([("../evil.jpg", b"x")], None),
            ([("sub/x.jpg", b"x")], None),
            ([("/abs.jpg", b"x")], None),
            ([("ok.jpg", b"x")], "link.jpg"),
        ],
    )
    def test_a_tar_with_anything_but_bare_files_is_refused_and_nothing_is_written(
        self, media, tmp_path, entries, symlink
    ):
        dest = tmp_path / "frames"
        dest.mkdir()
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})], artifact=_tar(entries, symlink=symlink))
        _install(client)

        with pytest.raises(NodeTaskError, match="unexpected entry"):
            _run(media, artifact_to=str(dest))

        assert os.listdir(dest) == []
        assert not (tmp_path / "evil.jpg").exists()

    def test_garbage_artifact_is_a_task_error(self, media, tmp_path):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})], artifact=b"not a tar at all")
        _install(client)
        with pytest.raises(NodeTaskError, match="couldn't be unpacked"):
            _run(media, artifact_to=str(tmp_path))

    def test_artifact_download_failure_is_node_unavailable(self, media, tmp_path):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])
        client.fetch_artifact = lambda task_id: (_ for _ in ()).throw(unreachable())
        registry = _install(client)
        with pytest.raises(NodeUnavailableError):
            _run(media, artifact_to=str(tmp_path))
        assert registry.reports == [("unavailable", NODE_ID)]


class TestNodeDrops:
    def test_three_failed_polls_in_a_row_make_the_node_unavailable(self, media):
        client = ScriptedClient([running(), unreachable(), unreachable(), unreachable()])
        registry = _install(client)

        with pytest.raises(NodeUnavailableError) as raised:
            _run(media)

        assert raised.value.node_id == NODE_ID and raised.value.node_name == NODE_NAME
        assert "stopped answering" in str(raised.value)
        assert len(client.named("poll")) == 4
        assert registry.reports == [("unavailable", NODE_ID)]
        assert client.named("delete") == []  # nobody to tell

    def test_two_failed_polls_then_an_answer_carry_on(self, media):
        client = ScriptedClient(
            [unreachable(), unreachable(), running(), unreachable(), finished(protocol.STATUS_DONE, result={"ok": 1})]
        )
        registry = _install(client)
        assert _run(media) == {"ok": 1}
        assert registry.reports == []

    def test_a_node_that_forgot_the_task_is_unavailable_at_once(self, media):
        client = ScriptedClient([running(), NodeUnknownTaskError("unknown_task")])
        registry = _install(client)
        with pytest.raises(NodeUnavailableError, match="no longer knows"):
            _run(media)
        assert registry.reports == [("unavailable", NODE_ID)]

    @pytest.mark.parametrize("error", [unreachable(), NodeAuthError("refused the key")])
    def test_a_node_that_wont_start_the_task_is_unavailable(self, media, error):
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})], start_error=error)
        registry = _install(client)
        with pytest.raises(NodeUnavailableError, match="couldn't start"):
            _run(media)
        assert registry.reports == [("unavailable", NODE_ID)]


class TestErrors:
    def test_file_not_visible_is_reported_and_raised_with_the_reason(self, media):
        error = {"kind": protocol.KIND_FILE_NOT_VISIBLE, "message": "no such file", "reason": "missing"}
        client = ScriptedClient([finished(protocol.STATUS_FILE_NOT_VISIBLE, error=error)])
        registry = _install(client)

        with pytest.raises(FileNotVisibleError) as raised:
            _run(media)

        assert (raised.value.node_id, raised.value.node_name, raised.value.reason) == (NODE_ID, NODE_NAME, "missing")
        assert registry.reports == [("not_visible", NODE_ID, media, "missing")]

    def test_codec_not_supported_raises_the_apps_exception_with_its_kind(self, media):
        error = {"kind": protocol.KIND_CODEC_NOT_SUPPORTED, "message": "GPU can't", "fallback_kind": FALLBACK_STALL}
        client = ScriptedClient([finished(protocol.STATUS_FAILED, error=error)])
        registry = _install(client)

        with pytest.raises(CodecNotSupportedError, match="GPU can't") as raised:
            _run(media, gpu=("NVIDIA", "cuda:0"))

        assert raised.value.kind == FALLBACK_STALL
        assert registry.reports == []

    def test_gpu_failure_reports_the_device_and_raises_codec_not_supported(self, media):
        error = {"kind": protocol.KIND_GPU_FAILED, "message": "cuda error"}
        client = ScriptedClient([finished(protocol.STATUS_FAILED, error=error)])
        registry = _install(client)

        with pytest.raises(CodecNotSupportedError) as raised:
            _run(media, gpu=("NVIDIA", "cuda:0"))

        assert raised.value.kind == FALLBACK_HWACCEL
        assert registry.reports == [("gpu_error", NODE_ID, "cuda:0")]

    def test_gpu_failure_naming_its_exception_is_left_to_the_op_to_raise_again(self, media):
        error = {"kind": protocol.KIND_GPU_FAILED, "message": "timed out", "error_type": "GpuAttemptFailedError"}
        client = ScriptedClient([finished(protocol.STATUS_FAILED, error=error)])
        registry = _install(client)

        with pytest.raises(NodeTaskError) as raised:
            _run(media, gpu=("NVIDIA", "cuda:0"))

        assert raised.value.error_type == "GpuAttemptFailedError" and raised.value.message == "timed out"
        assert registry.reports == [("gpu_error", NODE_ID, "cuda:0")]

    def test_failure_carries_the_nodes_message_type_and_extra(self, media):
        error = {"kind": "failed", "message": "boom", "error_type": "LoudnessError", "stderr_tail": ["a"]}
        _install(ScriptedClient([finished(protocol.STATUS_FAILED, error=error)]))

        with pytest.raises(NodeTaskError) as raised:
            _run(media)

        assert isinstance(raised.value, RuntimeError)
        assert (raised.value.kind, raised.value.message, raised.value.error_type) == ("failed", "boom", "LoudnessError")
        assert raised.value.extra == {"stderr_tail": ["a"]}


class TestCancelAndPause:
    def test_cancel_is_forwarded_and_the_cancelled_task_raises(self, media):
        cancel = threading.Event()

        def answer(client):
            if client.named("cancel"):
                return finished(protocol.STATUS_CANCELLED, error={"kind": "cancelled", "message": "stopped"})
            cancel.set()
            return running()

        client = ScriptedClient([answer])
        _install(client)

        with pytest.raises(NodeTaskCancelledError):
            _run(media, cancel_check=cancel.is_set)

        assert len(client.named("cancel")) == 1
        assert client.named("delete") == [("delete",)]

    def test_a_cancel_while_the_node_goes_away_ends_as_cancelled(self, media):
        cancel = threading.Event()
        cancel.set()
        client = ScriptedClient([unreachable()])
        _install(client)
        with pytest.raises(NodeTaskCancelledError):
            _run(media, cancel_check=cancel.is_set)

    def test_pause_and_resume_follow_the_pause_check(self, media):
        paused = {"now": False}
        polls = {"n": 0}

        def answer(client):
            polls["n"] += 1
            paused["now"] = polls["n"] in (2, 3)
            if polls["n"] >= 5:
                return finished(protocol.STATUS_DONE, result={})
            return running()

        client = ScriptedClient([answer])
        _install(client)

        _run(media, pause_check=lambda: paused["now"])

        controls = [c[0] for c in client.calls if c[0] in ("pause", "resume")]
        assert controls == ["pause", "resume"]

    def test_time_paused_on_the_node_counts_on_a_shared_freeze(self, media):
        from media_preview_generator.markers.freeze import Freeze

        paused = {"now": True}
        polls = {"n": 0}

        def answer(client):
            polls["n"] += 1
            if polls["n"] == 3:
                paused["now"] = False
            return finished(protocol.STATUS_DONE, result={}) if polls["n"] >= 4 else running()

        freeze = Freeze(lambda: paused["now"])
        _install(ScriptedClient([answer]))
        _run(media, pause_check=freeze)
        assert freeze.held_s > 0


class TestLogsAndProgress:
    def test_node_lines_land_in_the_jobs_log_with_the_node_and_device_prefix(self, media):
        lines = []
        state = running(log=[{"seq": 0, "level": "WARNING", "message": "decode {braces} slow"}], next_log=1)
        _install(ScriptedClient([state, finished(protocol.STATUS_DONE, result={})]))
        sink = logger.add(
            lambda m: lines.append((m.record["level"].name, m.record["message"])),
            filter=lambda r: is_job_thread_for(r["thread"].id, "job-1"),
            format="{message}",
        )
        try:

            def work():
                with job_thread("job-1"):
                    _run(media, gpu=("NVIDIA", "cuda:0"))

            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
        finally:
            logger.remove(sink)
        assert ("WARNING", "NAS/GPU0 · decode {braces} slow") in lines

    def test_cpu_tasks_are_prefixed_cpu_and_unknown_levels_log_at_info(self, media):
        lines = []
        state = running(log=[{"seq": 0, "level": "LOUD", "message": "hi"}], next_log=1)
        _install(ScriptedClient([state, finished(protocol.STATUS_DONE, result={})]))
        sink = logger.add(lambda m: lines.append((m.record["level"].name, m.record["message"])), format="{message}")
        try:
            _run(media)
        finally:
            logger.remove(sink)
        assert ("INFO", "NAS/CPU · hi") in lines

    def test_progress_is_handed_on_once_per_change(self, media):
        seen = []
        script = [
            running(progress={"percent": 10.0, "args": [1]}),
            running(progress={"percent": 10.0, "args": [1]}),
            running(progress={"percent": 50.0, "args": [5]}),
            finished(protocol.STATUS_DONE, result={}, progress={"percent": 100.0, "args": [10]}),
        ]
        _install(ScriptedClient(script))
        _run(media, progress_callback=seen.append)
        assert [p["percent"] for p in seen] == [10.0, 50.0, 100.0]

    def test_a_failing_progress_callback_never_fails_the_task(self, media):
        _install(ScriptedClient([finished(protocol.STATUS_DONE, result={"x": 1}, progress={"percent": 1.0})]))

        def boom(_state):
            raise ValueError("display")

        assert _run(media, progress_callback=boom) == {"x": 1}
