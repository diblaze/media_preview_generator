"""nodes.server.ops: each op calls the app's own heavy function with the node's path and device, and answers JSON."""

from __future__ import annotations

import json
import os
import tarfile

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
            (frames.GpuDecodeError("cuda died", stderr_tail=("line 1",)), protocol.KIND_GPU_FAILED, ["line 1"]),
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
