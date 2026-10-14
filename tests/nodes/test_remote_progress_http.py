"""Progress from a node reaches the worker: a real node over HTTP runs the real ops, the app's real NodeClient and
executor poll it, and each op's progress values, step text and CPU notes arrive at the local callbacks.

The lab run showed remote workers at 0% / 0.0x for a whole file: the node's task table turned the ``args`` list into
its text, so the app's relay found no list and dropped every update. Only the app functions the ops call are faked.
"""

from __future__ import annotations

import os
import threading
from functools import partial

import numpy as np
import pytest
import requests
from werkzeug.serving import make_server

from media_preview_generator.jobs.worker import Worker, WorkerPool
from media_preview_generator.loudness import analyze
from media_preview_generator.loudness.job import _stream_progress_reporter
from media_preview_generator.markers.audio import end_picture, fingerprint
from media_preview_generator.markers.credits import detector, textdet_helper
from media_preview_generator.nodes import executor, remote_ops
from media_preview_generator.nodes.client import NodeAddress, NodeClient
from media_preview_generator.nodes.executor import Placement, placed_on
from media_preview_generator.nodes.registry import set_registry
from media_preview_generator.nodes.server import app as node_app
from media_preview_generator.nodes.server import info, ops
from media_preview_generator.processing import generator

from .executor_fakes import NODE_ID, identity_registry, write_file

KEY = "progress-key"
FFMPEG = "/usr/bin/ffmpeg"


@pytest.fixture
def node(tmp_path, monkeypatch):
    for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_ROLES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-tmp"))
    monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": FFMPEG, "version": "ffmpeg version 7.1"})
    monkeypatch.setattr(ops, "_node_ffmpeg", lambda: FFMPEG)
    monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.02)
    app = node_app.create_node_app(config_dir=str(tmp_path / "config"), roles=["worker"], key=KEY)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False  # a proxy from the environment must not sit between the test and 127.0.0.1
    set_registry(
        identity_registry(NodeClient(NodeAddress(f"http://127.0.0.1:{server.server_port}", KEY), session=session))
    )
    yield
    set_registry(None)
    server.shutdown()
    thread.join(5)
    app.extensions[node_app.EXTENSION].close()


@pytest.fixture
def media(tmp_path):
    return write_file(str(tmp_path / "data" / "Show" / "S01E01.mkv"), b"0123456789")


def _on_node():
    return placed_on(Placement(NODE_ID, "NAS"))


def _worker_and_callback(worker_type: str = "GPU") -> tuple[Worker, partial]:
    pool = WorkerPool(cpu_workers=0, gpu_workers=0, selected_gpus=[])
    worker = Worker(0, worker_type, node_id=NODE_ID, node_name="NAS")
    return worker, partial(pool._update_worker_progress, worker)


class _Config:
    plex_bif_frame_interval = 5
    tonemap_algorithm = "hable"
    thumbnail_quality = 4
    ffmpeg_threads = 2
    log_level = "INFO"
    server_display_name = "Plex A"


class TestPreviewProgress:
    def test_ffmpeg_progress_on_the_node_fills_the_workers_progress_fields(self, node, media, tmp_path, monkeypatch):
        def fake(video, out, gpu, device, config, progress, **kwargs):
            write_file(os.path.join(out, "0000000000.jpg"), b"frame")
            progress(40.0, 4.0, 10.0, "1.5x", 6.0, 20, 5.0, 2.0, 10, "00:00:04.00", 9.0, media_file=video)
            return (True, 1, True, 3.25, "1.5x", "")

        monkeypatch.setattr(generator, "generate_images", fake)
        worker, callback = _worker_and_callback()
        slot = tmp_path / "slot"
        slot.mkdir()

        with _on_node():
            remote_ops.generate_images(media, str(slot), "NVIDIA", "cuda:0", _Config(), callback)

        assert (worker.progress_percent, worker.speed, worker.remaining_time) == (40.0, "1.5x", 6.0)
        assert (worker.current_duration, worker.total_duration) == (4.0, 10.0)
        assert (worker.frame, worker.fps, worker.time_str) == (20, 5.0, "00:00:04.00")
        assert worker.media_file == media  # the app's path, never the node's
        assert worker.ffmpeg_started is True


class TestLoudnessProgress:
    def test_the_nodes_seconds_and_speed_reach_the_worker(self, node, media, monkeypatch):
        monkeypatch.setattr(analyze, "resolve_ffmpeg", lambda fallback: FFMPEG)

        def fake_run(ffmpeg, path, index, **kwargs):
            kwargs["on_progress"](12.0, 3.0)
            return {"ln:loudness": "-20.00"}

        monkeypatch.setattr(analyze, "run", fake_run)
        worker, callback = _worker_and_callback("CPU")
        report = _stream_progress_reporter(callback, n=1, streams=1, durations_s=[24.0])

        with _on_node():
            remote_ops.loudness_run(FFMPEG, media, 2, duration_ms=24000, codec="aac", on_progress=report)

        assert (worker.progress_percent, worker.speed) == (50.0, "3.0x")
        assert (worker.current_duration, worker.total_duration) == (12.0, 24.0)


class TestMarkersProgress:
    def test_fingerprint_progress_reaches_the_step(self, node, media, monkeypatch):
        monkeypatch.setattr(fingerprint, "chromaprint_ffmpeg", lambda configured: FFMPEG)

        def fake(path, duration_ms, **kwargs):
            kwargs["progress"](120.0, lambda: 0.0).update(60.0)
            return np.array([7, 8, 9], dtype="<u4")

        monkeypatch.setattr(fingerprint, "compute_fingerprint", fake)
        made: list[tuple[float, list[float]]] = []

        class Step:
            def __init__(self, total_s: float) -> None:
                self.done: list[float] = []
                made.append((total_s, self.done))

            def update(self, done_s: float) -> None:
                self.done.append(done_s)

        with _on_node():
            remote_ops.compute_fingerprint(media, 343000, ffmpeg=FFMPEG, progress=lambda total_s, clock: Step(total_s))

        assert made == [(120.0, [60.0])]

    def test_end_picture_progress_and_cpu_notes_arrive(self, node, media, monkeypatch):
        frame = np.zeros((4, 4), dtype=np.float32)

        def fake(path, start_s, length_s, **kwargs):
            kwargs["fallback_callback"]("the GPU couldn't decode it; CPU this time")
            kwargs["progress_callback"](50.0, 3.0, 6.0, "2.0x")
            return [(1.0, frame)]

        monkeypatch.setattr(end_picture, "decode_frames", fake)
        notes, progress = [], []

        with _on_node():
            remote_ops.decode_frames(
                media, 0.0, 6.0, ffmpeg=FFMPEG, gpu="NVIDIA", gpu_device_path="cuda:0", container_start_s=0.0,
                download_format=None, fallback_callback=notes.append, progress_callback=lambda *a: progress.append(a),
            )  # fmt: skip

        assert notes == ["the GPU couldn't decode it; CPU this time"]
        assert progress == [(50.0, 3.0, 6.0, "2.0x")]

    def test_credits_phase_cpu_notes_and_progress_arrive(self, node, media, monkeypatch):
        class Pool:
            def detect_boxes(self, planes, **kwargs):
                kwargs["on_cpu"]("text detection ran on the CPU")
                return []

        def fake(path, **kwargs):
            kwargs["phase"]("Reading the credits…")
            kwargs["detect_boxes"](None)
            kwargs["progress_callback"](25.0, 30.0, 120.0, "4.0x")
            return detector.CreditsTextResult(start_s=100.0, end_s=None, key_rows=(), fine_rows=(), end_rows=(),
                                              overlays=(), scale=1)  # fmt: skip

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", Pool)
        monkeypatch.setattr(detector, "find_credits", fake)
        phases, notes, progress = [], [], []

        with _on_node():
            remote_ops.find_credits(
                media, duration_ms=200_000, is_episode=True, ffmpeg=FFMPEG, gpu="NVIDIA", gpu_device_path="cuda:0",
                phase=phases.append, on_cpu=notes.append, progress_callback=lambda *a: progress.append(a),
            )  # fmt: skip

        assert phases == ["Reading the credits…"]
        assert notes == ["text detection ran on the CPU"]
        assert progress == [(25.0, 30.0, 120.0, "4.0x")]
