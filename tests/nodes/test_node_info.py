"""The worker block: GPU detection made JSON-safe, cached, re-checked; CPU/RAM guards; credit text when known."""

from __future__ import annotations

import sys
import threading
import time
import types

from media_preview_generator.nodes.server import info


def _until(check, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting")


class TestDetection:
    def test_cached_until_rescan(self, tmp_path):
        calls = []
        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=lambda: calls.append(1) or [])

        worker.gpus()
        worker.gpus()
        assert len(calls) == 1

        worker.rescan()
        assert len(calls) == 2

    def test_gpu_error_triggers_a_background_recheck(self, tmp_path):
        calls = []
        worker = info.WorkerInfo(temp_root=str(tmp_path), recheck_s=3600, detect=lambda: calls.append(1) or [])
        worker.start()
        try:
            _until(lambda: len(calls) == 1)

            worker.note_gpu_error()

            _until(lambda: len(calls) == 2)
        finally:
            worker.stop()

    def test_a_failing_detection_doesnt_end_the_rechecks(self, tmp_path):
        calls = []
        failed = threading.Event()

        def detect():
            calls.append(1)
            if len(calls) == 1:
                failed.set()
                raise RuntimeError("probe crashed")
            return []

        worker = info.WorkerInfo(temp_root=str(tmp_path), recheck_s=3600, detect=detect)
        worker.start()
        try:
            failed.wait(10)
            worker.note_gpu_error()
            _until(lambda: len(calls) == 2)
        finally:
            worker.stop()

    def test_entries_are_json_safe(self):
        entry = info._gpu_entry(("AMD", "/dev/dri/renderD128", {"status": "failed", "error": ValueError("x")}))

        assert entry == {"type": "AMD", "device": "/dev/dri/renderD128", "name": "AMD", "status": "failed",
                         "error": "x"}  # fmt: skip


class TestHost:
    def test_meminfo_read_in_bytes(self, tmp_path):
        path = tmp_path / "meminfo"
        path.write_text("MemTotal:       16000 kB\nMemFree: 1 kB\nMemAvailable:    8000 kB\n")

        assert info._meminfo(str(path)) == (16000 * 1024, 8000 * 1024)

    def test_meminfo_missing_is_none(self, tmp_path):
        assert info._meminfo(str(tmp_path / "nope")) == (None, None)

    def test_load1_none_without_a_load_average(self, monkeypatch):
        def no_load():
            raise OSError("unavailable")

        monkeypatch.setattr(info.os, "getloadavg", no_load)

        assert info._load1() is None

    def test_ffmpeg_uses_the_apps_resolution(self, monkeypatch):
        import media_preview_generator.config as config

        monkeypatch.setattr(config, "_resolve_ffmpeg_path", lambda: "/opt/ff/ffmpeg")
        monkeypatch.setattr(
            info.subprocess,
            "run",
            lambda *a, **k: types.SimpleNamespace(stdout="ffmpeg version 7.1 Copyright\nbuilt with gcc\n"),
        )

        assert info._ffmpeg() == {"path": "/opt/ff/ffmpeg", "version": "ffmpeg version 7.1 Copyright"}

    def test_no_ffmpeg(self, monkeypatch):
        import media_preview_generator.config as config

        monkeypatch.setattr(config, "_resolve_ffmpeg_path", lambda: None)

        assert info._ffmpeg() == {"path": None, "version": None}


class TestCreditText:
    def _fake_textdet(self, monkeypatch, pool):
        fake = types.ModuleType("media_preview_generator.markers.credits.textdet_helper")
        fake._pool_lock = threading.Lock()
        fake._pool = pool
        fake.device_key = lambda gpu, device: device or gpu
        monkeypatch.setitem(sys.modules, fake.__name__, fake)

    def test_absent_until_text_detection_has_run(self, monkeypatch, tmp_path):
        monkeypatch.delitem(sys.modules, "media_preview_generator.markers.credits.textdet_helper", raising=False)
        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=lambda: [("NVIDIA", "cuda:0", {})])

        assert "credit_text" not in worker.gpus()[0]

    def test_reported_from_the_running_pool(self, monkeypatch, tmp_path):
        backends = {"cuda:0": "webgpu", "/dev/dri/renderD128": "cpu", "/dev/dri/renderD129": None}
        pool = types.SimpleNamespace(
            backend_of=lambda gpu, device: backends[device],
            credits_on_cpu=lambda: [{"key": "/dev/dri/renderD128", "reason": "self-test found other boxes"}],
        )
        self._fake_textdet(monkeypatch, pool)
        detected = [("NVIDIA", "cuda:0", {}), ("AMD", "/dev/dri/renderD128", {}), ("INTEL", "/dev/dri/renderD129", {})]
        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=lambda: detected)

        nvidia, amd, intel = worker.gpus()

        assert nvidia["credit_text"] == {"on_gpu": True, "reason": ""}
        assert amd["credit_text"] == {"on_gpu": False, "reason": "self-test found other boxes"}
        assert "credit_text" not in intel
