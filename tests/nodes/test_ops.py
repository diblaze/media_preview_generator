"""nodes.server.ops: each op calls the app's own heavy function with the node's path and device, and answers JSON."""

from __future__ import annotations

import json
import os
import tarfile
import threading
import time

import numpy as np
import pytest

from media_preview_generator.loudness import analyze
from media_preview_generator.markers.audio import end_picture, fingerprint
from media_preview_generator.markers.credits import detector, frames, textdet_helper
from media_preview_generator.nodes import protocol
from media_preview_generator.nodes.protocol import OpError
from media_preview_generator.nodes.server import ops
from media_preview_generator.processing import generator
from media_preview_generator.processing.generator import FALLBACK_CODEC, FALLBACK_HWACCEL, CodecNotSupportedError

from .executor_fakes import FakeTaskContext

NODE_PATH = "/mnt/media/Show/S01E01.mkv"
GPU = ("NVIDIA", "cuda:0")


@pytest.fixture
def ctx(tmp_path):
    return FakeTaskContext(str(tmp_path), GPU)


@pytest.fixture(autouse=True)
def _node_ffmpeg(monkeypatch):
    monkeypatch.setattr(ops, "_node_ffmpeg", lambda: "/node/ffmpeg")
    # credits_find points the process pool's CPU helper count at the node's own tasks; undone after each test.
    monkeypatch.setattr(textdet_helper, "_cpu_limit_source", None)
    monkeypatch.setattr(ops, "_cpu_credits_running", 0)
    monkeypatch.setattr(ops, "_cpu_credits_most", 0)


def test_every_protocol_op_is_registered():
    assert set(ops.OPS) == set(protocol.OPS)


class TestPreviewFrames:
    PARAMS = {
        "frame_interval": 5,
        "tonemap_algorithm": "hable",
        "thumbnail_quality": 4,
        "ffmpeg_threads": 3,
        "log_level": "DEBUG",
        "server_display_name": None,
        "ffmpeg_threads_override": 2,
    }

    def test_frames_become_a_sorted_tar_and_the_tuple_comes_back(self, ctx, monkeypatch):
        seen = {}

        def fake(video, out, gpu, device, config, progress, **kwargs):
            seen.update(video=video, gpu=gpu, device=device, config=config, **kwargs)
            for name in ("0000000005.jpg", "0000000000.jpg"):
                with open(os.path.join(out, name), "wb") as fh:
                    fh.write(name.encode())
            progress(50.0, 6.0, 12.0, "2.0x", 3.0, 30, 10.0, 2.0, 100, "00:00:06.00", 1.0, media_file=video)
            generator.record_failure(video, 1, "first try failed", "GPU")
            return (True, 2, True, 1.5, "2.0x", "")

        monkeypatch.setattr(generator, "generate_images", fake)
        result = ops.preview_frames(ctx, NODE_PATH, self.PARAMS)

        assert seen["video"] == NODE_PATH and (seen["gpu"], seen["device"]) == GPU
        assert seen["ffmpeg_threads_override"] == 2
        assert seen["cancel_check"] == ctx.cancel_check and seen["pause_check"] == ctx.pause_check
        config = seen["config"]
        assert (config.plex_bif_frame_interval, config.tonemap_algorithm, config.thumbnail_quality) == (5, "hable", 4)
        assert (config.ffmpeg_threads, config.log_level, config.ffmpeg_path) == (3, "DEBUG", "/node/ffmpeg")
        assert config.server_display_name is None
        assert result["generate_images"] == [True, 2, True, 1.5, "2.0x", ""]
        assert result["frames"] == 2
        assert result["failures"] == [{"exit_code": 1, "reason": "first try failed", "worker_type": "GPU"}]
        json.dumps(result)
        with tarfile.open(ctx.artifact) as tar:
            assert tar.getnames() == ["0000000000.jpg", "0000000005.jpg"]
        assert ctx.progress_calls[-1] == {
            "percent": 50.0,
            "args": [6.0, 12.0, "2.0x", 3.0, 30, 10.0, 2.0, 100, "00:00:06.00", 1.0],
        }

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [(FALLBACK_CODEC, protocol.KIND_CODEC_NOT_SUPPORTED), (FALLBACK_HWACCEL, protocol.KIND_GPU_FAILED)],
    )
    def test_a_gpu_hand_off_keeps_its_kind(self, ctx, monkeypatch, kind, expected):
        def fake(*args, **kwargs):
            raise CodecNotSupportedError("GPU processing failed", kind=kind)

        monkeypatch.setattr(generator, "generate_images", fake)
        with pytest.raises(OpError) as raised:
            ops.preview_frames(ctx, NODE_PATH, self.PARAMS)
        assert raised.value.kind == expected and raised.value.extra == {"fallback_kind": kind}

    def test_cancellation_is_a_cancelled_task(self, ctx, monkeypatch):
        def fake(*args, **kwargs):
            raise generator.CancellationError("Cancelled")

        monkeypatch.setattr(generator, "generate_images", fake)
        with pytest.raises(OpError) as raised:
            ops.preview_frames(ctx, NODE_PATH, self.PARAMS)
        assert raised.value.kind == protocol.KIND_CANCELLED
        assert raised.value.extra["error_type"] == "CancellationError"

    @pytest.mark.parametrize("value", ["hable,drawtext=text=x", "HABLE", "", None, 3])
    def test_a_tonemap_the_app_cant_send_runs_no_ffmpeg(self, ctx, monkeypatch, value):
        ran = []
        monkeypatch.setattr(generator, "generate_images", lambda *a, **k: ran.append(1))

        with pytest.raises(OpError) as raised:
            ops.preview_frames(ctx, NODE_PATH, {**self.PARAMS, "tonemap_algorithm": value})

        assert raised.value.kind == protocol.KIND_FAILED and "tonemap_algorithm must be one of" in raised.value.message
        assert ran == []


class TestLoudness:
    def test_one_track_runs_analyze_run_with_the_nodes_loudness_ffmpeg(self, ctx, monkeypatch):
        seen = {}

        def fake_run(ffmpeg, path, index, **kwargs):
            seen.update(ffmpeg=ffmpeg, path=path, index=index, **kwargs)
            kwargs["on_progress"](30.0, 2.0)
            return {"ln:loudness": "-23.00"}

        monkeypatch.setattr(analyze, "run", fake_run)
        monkeypatch.setattr(analyze, "resolve_ffmpeg", lambda fallback: f"loudness:{fallback}")
        result = ops.loudness_analyze(ctx, NODE_PATH, {"tracks": [[2, "eac3", 60000]], "batch": False})

        assert result == {"fields": {"ln:loudness": "-23.00"}}
        assert (seen["ffmpeg"], seen["path"], seen["index"]) == ("loudness:/node/ffmpeg", NODE_PATH, 2)
        assert (seen["codec"], seen["duration_ms"]) == ("eac3", 60000)
        assert ctx.progress_calls[-1] == {"percent": 50.0, "args": [30.0, 2.0]}

    def test_a_batch_returns_each_streams_fields_or_error(self, ctx, monkeypatch):
        def fake_many(ffmpeg, path, tracks, **kwargs):
            assert tracks == [(1, "aac", 1000), (3, "eac3", None)]
            return {1: {"ln:peak": "-1.00"}, 3: analyze.LoudnessNoDecoder("no decoder for 3")}

        monkeypatch.setattr(analyze, "run_many", fake_many)
        result = ops.loudness_analyze(ctx, NODE_PATH, {"tracks": [[1, "aac", 1000], [3, "eac3", None]], "batch": True})

        assert result == {
            "results": [
                {"index": 1, "fields": {"ln:peak": "-1.00"}},
                {"index": 3, "error_type": "LoudnessNoDecoder", "message": "no decoder for 3"},
            ]
        }

    @pytest.mark.parametrize(("cancelled", "kind"), [(False, protocol.KIND_FAILED), (True, protocol.KIND_CANCELLED)])
    def test_a_raised_loudness_error_keeps_its_class_name(self, ctx, monkeypatch, cancelled, kind):
        ctx.cancelled = cancelled

        def fake_run(*args, **kwargs):
            raise analyze.LoudnessTimeout("timed out after 600 s")

        monkeypatch.setattr(analyze, "run", fake_run)
        with pytest.raises(OpError) as raised:
            ops.loudness_analyze(ctx, NODE_PATH, {"tracks": [[1, "aac", 1000]]})
        assert raised.value.kind == kind
        assert raised.value.extra == {"error_type": "LoudnessTimeout"}


class TestFingerprint:
    def test_points_go_back_as_little_endian_u32(self, ctx, monkeypatch):
        seen = {}

        def fake(path, duration_ms, **kwargs):
            seen.update(path=path, duration_ms=duration_ms, **kwargs)
            step = kwargs["progress"](100.0, lambda: 0.0)
            step.update(25.0)
            return np.array([1, 2, 0xFFFFFFFF], dtype="<u4")

        monkeypatch.setattr(fingerprint, "chromaprint_ffmpeg", lambda configured: f"chroma:{configured}")
        monkeypatch.setattr(fingerprint, "compute_fingerprint", fake)
        result = ops.intro_fingerprint(ctx, NODE_PATH, {"duration_ms": 600000, "retime": 1.04, "progress": True})

        json.dumps(result)
        assert protocol.decode_array(result["points"]).tolist() == [1, 2, 0xFFFFFFFF]
        assert (seen["path"], seen["duration_ms"], seen["ffmpeg"], seen["retime"]) == (
            NODE_PATH,
            600000,
            "chroma:/node/ffmpeg",
            1.04,
        )
        assert ctx.progress_calls[-1]["args"][:2] == [25.0, 100.0]

    def test_no_progress_wanted_sends_none(self, ctx, monkeypatch):
        seen = {}
        monkeypatch.setattr(fingerprint, "chromaprint_ffmpeg", lambda configured: "chroma")
        monkeypatch.setattr(
            fingerprint, "compute_fingerprint", lambda *a, **k: seen.update(k) or np.zeros(0, dtype="<u4")
        )
        ops.intro_fingerprint(ctx, NODE_PATH, {"duration_ms": 1000})
        assert seen["progress"] is None

    def test_a_node_without_chromaprint_fails_as_a_fingerprint_error(self, ctx, monkeypatch):
        monkeypatch.setattr(fingerprint, "chromaprint_ffmpeg", lambda configured: None)
        with pytest.raises(OpError) as raised:
            ops.intro_fingerprint(ctx, NODE_PATH, {"duration_ms": 1000})
        assert raised.value.extra == {"error_type": "FingerprintError"}

    def test_a_stalled_mount_keeps_its_class(self, ctx, monkeypatch):
        monkeypatch.setattr(fingerprint, "chromaprint_ffmpeg", lambda configured: "chroma")

        def fake(*args, **kwargs):
            raise fingerprint.FingerprintStalledError("stuck")

        monkeypatch.setattr(fingerprint, "compute_fingerprint", fake)
        with pytest.raises(OpError) as raised:
            ops.intro_fingerprint(ctx, NODE_PATH, {"duration_ms": 1000})
        assert raised.value.extra == {"error_type": "FingerprintStalledError"}


class TestEndPicture:
    PARAMS = {
        "start_s": 10.0,
        "length_s": 4.0,
        "container_start_s": 0.5,
        "download_format": "nv12",
        "ffmpeg_threads": 2,
        "progress": True,
    }

    def test_frames_go_back_as_f32_with_their_times_and_fallbacks(self, ctx, monkeypatch):
        seen = {}
        frame = np.arange(64 * 36, dtype=np.float32).reshape(36, 64) / 7

        def fake(path, start_s, length_s, **kwargs):
            seen.update(path=path, start_s=start_s, length_s=length_s, **kwargs)
            kwargs["fallback_callback"]("decoded on the CPU")
            kwargs["progress_callback"](50.0, 2.0, 4.0, "1.0x", 2.0)
            return [(10.25, frame), (10.75, frame + 1)]

        monkeypatch.setattr(end_picture, "decode_frames", fake)
        result = ops.intro_end_picture(ctx, NODE_PATH, self.PARAMS)

        json.dumps(result)
        assert [row["t"] for row in result["frames"]] == [10.25, 10.75]
        assert np.array_equal(protocol.decode_array(result["frames"][1]["frame"]), frame + 1)
        assert (seen["gpu"], seen["gpu_device_path"], seen["ffmpeg"]) == ("NVIDIA", "cuda:0", "/node/ffmpeg")
        assert (seen["container_start_s"], seen["download_format"], seen["ffmpeg_threads"]) == (0.5, "nv12", 2)
        assert ctx.progress_calls[-1] == {
            "percent": 50.0,
            "args": [2.0, 4.0, "1.0x", 2.0],
            "fallbacks": ["decoded on the CPU"],
        }

    @pytest.mark.parametrize(
        ("error", "kind"),
        [
            (end_picture.GpuAttemptFailedError("GPU timed out"), protocol.KIND_GPU_FAILED),
            (frames.DecodeTimeoutError("CPU timed out"), protocol.KIND_FAILED),
            (frames.NoDecoderError("no decoder"), protocol.KIND_FAILED),
        ],
    )
    def test_decode_errors_keep_their_class(self, ctx, monkeypatch, error, kind):
        def fake(*args, **kwargs):
            raise error

        monkeypatch.setattr(end_picture, "decode_frames", fake)
        with pytest.raises(OpError) as raised:
            ops.intro_end_picture(ctx, NODE_PATH, self.PARAMS)
        assert raised.value.kind == kind and raised.value.extra["error_type"] == type(error).__name__

    def test_a_gpu_attempt_on_the_nodes_cpu_is_no_gpu_failure(self, tmp_path, monkeypatch):
        ctx = FakeTaskContext(str(tmp_path), None)

        def fake(*args, **kwargs):
            raise end_picture.GpuAttemptFailedError("x")

        monkeypatch.setattr(end_picture, "decode_frames", fake)
        with pytest.raises(OpError) as raised:
            ops.intro_end_picture(ctx, NODE_PATH, self.PARAMS)
        assert raised.value.kind == protocol.KIND_FAILED

    @pytest.mark.parametrize("value", ["nv12,drawtext=text=x", "yuv420p", 1])
    def test_a_download_format_the_app_cant_send_runs_no_ffmpeg(self, ctx, monkeypatch, value):
        ran = []
        monkeypatch.setattr(end_picture, "decode_frames", lambda *a, **k: ran.append(1))

        with pytest.raises(OpError) as raised:
            ops.intro_end_picture(ctx, NODE_PATH, {**self.PARAMS, "download_format": value})

        assert raised.value.kind == protocol.KIND_FAILED and ran == []

    def test_no_download_format_is_allowed(self, ctx, monkeypatch):
        seen = {}
        monkeypatch.setattr(end_picture, "decode_frames", lambda *a, **k: seen.update(k) or [])

        ops.intro_end_picture(ctx, NODE_PATH, {**self.PARAMS, "download_format": None})

        assert seen["download_format"] is None


class _Pool:
    def __init__(self):
        self.calls = []

    def detect_boxes(self, planes, **kwargs):
        self.calls.append(("boxes", kwargs))
        kwargs["on_cpu"]("text detection ran on the CPU")
        return [()] * len(planes)

    def read_text(self, planes, **kwargs):
        self.calls.append(("text", kwargs))
        return [[]] * len(planes)


class TestCredits:
    PARAMS = {
        "duration_ms": 2_400_000,
        "is_episode": True,
        "tail_s": None,
        "earliest_start_s": 2100.0,
        "ffmpeg_threads": 4,
        "gpu_worker": True,
        "read_text": True,
        "progress": True,
    }

    def test_find_credits_runs_with_the_nodes_own_pool_and_the_result_is_json(self, ctx, monkeypatch):
        pool = _Pool()
        seen = {}
        result = detector.CreditsTextResult(
            start_s=2200.5,
            end_s=None,
            key_rows=((2190.0, 0, 3.5, ()), (2200.0, 4, 30.25, ((1, 2, 3, 4),))),
            fine_rows=((2199.0, 2, 20.0, ((5, 6, 7, 8),)),),
            end_rows=(),
            overlays=((9, 9, 9, 9),),
            scale=2,
            run_rows=((2200.0, 4, 30.25),),
            prose_start_s=None,
        )

        def fake(path, **kwargs):
            seen.update(path=path, **kwargs)
            kwargs["phase"]("Reading credits…")
            kwargs["detect_boxes"](np.zeros((2, 4, 4), dtype=np.uint8))
            kwargs["read_text"](np.zeros((1, 4, 4), dtype=np.uint8))
            return result

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", lambda: pool)
        monkeypatch.setattr(detector, "find_credits", fake)
        answer = ops.credits_find(ctx, NODE_PATH, self.PARAMS)

        json.dumps(answer)
        from media_preview_generator.nodes.remote_ops import credits_result

        assert credits_result(answer["result"]) == result
        assert (seen["path"], seen["gpu"], seen["gpu_device_path"], seen["ffmpeg"]) == (NODE_PATH, *GPU, "/node/ffmpeg")
        assert (seen["earliest_start_s"], seen["ffmpeg_threads"], seen["is_episode"]) == (2100.0, 4, True)
        assert pool.calls[0][1]["gpu_worker"] is True and pool.calls[0][1]["gpu"] == "NVIDIA"
        assert ctx.progress_calls[-1]["phase"] == "Reading credits…"
        assert ctx.progress_calls[-1]["fallbacks"] == ["text detection ran on the CPU"]

    def test_no_card_reading_asked_passes_no_reader(self, ctx, monkeypatch):
        seen = {}
        monkeypatch.setattr(textdet_helper, "get_textdet_pool", _Pool)
        monkeypatch.setattr(
            detector,
            "find_credits",
            lambda path, **kw: seen.update(kw) or detector.CreditsTextResult(None, None, (), (), ()),
        )
        ops.credits_find(ctx, NODE_PATH, {**self.PARAMS, "read_text": False})
        assert seen["read_text"] is None

    @pytest.mark.parametrize(
        ("error", "kind", "extra"),
        [
            (
                frames.GpuDecodeError("cuda died", stderr_tail=("line 1",), fallback_kind=FALLBACK_HWACCEL),
                protocol.KIND_GPU_FAILED,
                ["line 1"],
            ),
            (frames.GpuReadNothingError(), protocol.KIND_FAILED, None),
            (textdet_helper.TextDetUnavailableError("helper gone"), protocol.KIND_FAILED, None),
        ],
    )
    def test_errors_keep_their_class(self, ctx, monkeypatch, error, kind, extra):
        def fake(path, **kwargs):
            raise error

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", _Pool)
        monkeypatch.setattr(detector, "find_credits", fake)
        with pytest.raises(OpError) as raised:
            ops.credits_find(ctx, NODE_PATH, self.PARAMS)
        assert raised.value.kind == kind
        assert raised.value.extra["error_type"] == type(error).__name__
        assert raised.value.extra.get("stderr_tail") == extra

    @pytest.mark.parametrize(
        ("fallback_kind", "kind"),
        [
            (FALLBACK_HWACCEL, protocol.KIND_GPU_FAILED),
            (generator.FALLBACK_SIGNAL, protocol.KIND_GPU_FAILED),
            # A codec the GPU can't decode (AV1 on a Pascal card), an I/O error or an unnamed failure is no reason to
            # test the GPU again: the file only needs the CPU, as preview.frames says for the same file.
            (FALLBACK_CODEC, protocol.KIND_FAILED),
            (generator.FALLBACK_IO_ERROR, protocol.KIND_FAILED),
            (None, protocol.KIND_FAILED),
        ],
    )
    def test_only_a_gpu_fault_is_reported_as_the_gpu_failing(self, ctx, monkeypatch, fallback_kind, kind):
        def fake(path, **kwargs):
            raise frames.GpuDecodeError("the GPU said no", stderr_tail=("why",), fallback_kind=fallback_kind)

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", _Pool)
        monkeypatch.setattr(detector, "find_credits", fake)
        with pytest.raises(OpError) as raised:
            ops.credits_find(ctx, NODE_PATH, self.PARAMS)

        assert raised.value.kind == kind
        assert raised.value.extra["error_type"] == "GpuDecodeError"
        assert raised.value.extra.get("fallback_kind") == fallback_kind

    def test_two_cpu_tasks_on_a_node_detect_text_side_by_side(self, tmp_path, monkeypatch):
        # A node process: no saved settings, a fresh process pool whose helpers are faked to take a second each.
        from media_preview_generator.web import settings_manager

        monkeypatch.setattr(settings_manager, "peek_settings_manager", lambda: None)
        monkeypatch.setattr(textdet_helper, "_pool", None)
        lock = threading.Lock()
        inside = {"now": 0, "most": 0}

        def on_helper(self, key, planes, spec, accept=None, read=False):
            with lock:
                inside["now"] += 1
                inside["most"] = max(inside["most"], inside["now"])
            time.sleep(1.0)
            with lock:
                inside["now"] -= 1
            return [() for _ in range(len(planes))]

        def find_credits(path, *, detect_boxes, **_kwargs):
            detect_boxes(np.zeros((1, 8, 8), dtype=np.uint8))
            return detector.CreditsTextResult(None, None, (), (), ())

        monkeypatch.setattr(textdet_helper.TextDetectorPool, "_on_helper", on_helper)
        monkeypatch.setattr(detector, "find_credits", find_credits)
        params = {**self.PARAMS, "gpu_worker": False}
        errors: list[BaseException] = []

        def run() -> None:
            try:
                ops.credits_find(FakeTaskContext(str(tmp_path), None), NODE_PATH, params)
            except BaseException as exc:  # noqa: BLE001 - reported by the assert below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
        finally:
            if textdet_helper._pool is not None:
                textdet_helper._pool.close_all()

        assert errors == []
        assert inside["most"] == 2, f"text detection ran {inside['most']} at a time for two CPU tasks"
        assert ops._cpu_credits_running == 0 and ops._cpu_credits_cap() == 2

    def test_a_gpu_workers_task_isnt_counted_against_the_cpu_cap(self, ctx, monkeypatch):
        seen = []
        monkeypatch.setattr(textdet_helper, "get_textdet_pool", _Pool)
        monkeypatch.setattr(
            detector,
            "find_credits",
            lambda path, **kw: (
                seen.append(ops._cpu_credits_cap()) or detector.CreditsTextResult(None, None, (), (), ())
            ),
        )

        ops.credits_find(ctx, NODE_PATH, {**self.PARAMS, "gpu_worker": True})
        ops.credits_find(ctx, NODE_PATH, {**self.PARAMS, "gpu_worker": False})

        assert seen == [0, 1] and textdet_helper._cpu_limit() == 1

    def test_a_tail_that_isnt_a_number_runs_no_decode(self, ctx, monkeypatch):
        ran = []
        monkeypatch.setattr(textdet_helper, "get_textdet_pool", _Pool)
        monkeypatch.setattr(detector, "find_credits", lambda *a, **k: ran.append(1))

        with pytest.raises(ValueError):
            ops.credits_find(ctx, NODE_PATH, {**self.PARAMS, "tail_s": "30,drawtext=text=x"})

        assert ran == []


class TestANodeWithoutASystemTempFolder:
    """A node run read-only, with only NODE_TEMP_DIR writable: FFmpeg's log and the helpers' stderr files go there.

    They went to the system temp folder, so every preview on such a node failed with "No usable temporary directory
    found" while the node itself checked NODE_TEMP_DIR at start and reported itself ready.
    """

    @pytest.fixture(autouse=True)
    def read_only_node(self, tmp_path, monkeypatch):
        import tempfile

        node_temp = tmp_path / "node-tmp"
        node_temp.mkdir()
        monkeypatch.setenv("MODE", "node")
        monkeypatch.setenv("NODE_TEMP_DIR", str(node_temp))
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "no-system-temp"))  # what gettempdir() answers
        return node_temp

    def test_ffmpegs_log_is_written_to_the_nodes_temp_folder(self, read_only_node, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from media_preview_generator.processing import ffmpeg_runner

        config = SimpleNamespace(ffmpeg_path="/usr/bin/ffmpeg", thumbnail_quality=4, log_level="INFO", ffmpeg_threads=2)
        runner = ffmpeg_runner.create_ffmpeg_runner(
            video_file="/media/Movie (2020)/Movie.mkv",
            output_folder=str(read_only_node / "frames"),
            gpu=None,
            gpu_device_path=None,
            config=config,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            path_kind="sdr",
            libplacebo_vf="",
            use_libplacebo=False,
            dv5_software_fallback=False,
            base_scale="scale=w=320:h=240",
            fps_filter="fps=fps=0.1:round=up:start_time=0",
            hdr10_zscale_chain="",
        )
        logs = []
        proc = MagicMock(returncode=0, pid=4242)
        proc.poll.return_value = 0

        def popen(args, **kwargs):
            logs.append(kwargs["stderr"].name)
            return proc

        monkeypatch.setattr(ffmpeg_runner.subprocess, "Popen", popen)
        monkeypatch.setattr(ffmpeg_runner.time, "sleep", lambda _s: None)
        monkeypatch.setattr(ffmpeg_runner, "dies_with_node", lambda args: (args, None))  # no setpriv lookup

        runner(use_skip=False)

        assert len(logs) == 1 and os.path.dirname(logs[0]) == str(read_only_node)

    def test_a_credit_decode_keeps_ffmpegs_stderr_in_the_nodes_temp_folder(self, monkeypatch):
        def no_ffmpeg(*args, **kwargs):
            raise OSError("no ffmpeg here")

        monkeypatch.setattr(frames.subprocess, "Popen", no_ffmpeg)

        # The decode gets as far as starting FFmpeg: its stderr file was made.
        with pytest.raises(frames.FrameDecodeError, match="could not run"):
            frames.run_decode(["ffmpeg"], hw_active=False, detect_boxes=lambda planes: [], pts_offset_s=0.0)

    def test_a_text_detection_helper_keeps_its_stderr_in_the_nodes_temp_folder(self):
        def no_helper(*args, **kwargs):
            raise OSError("no python here")

        spec = textdet_helper.HelperSpec("cpu", "cpu", None, False, {})

        # The helper gets as far as starting: its stderr file was made.
        with pytest.raises(textdet_helper.HelperStartError, match="couldn't start"):
            textdet_helper._start(
                spec, command=lambda _spec: ["helper"], popen=no_helper, start_timeout_s=1.0, request_timeout_s=1.0
            )
