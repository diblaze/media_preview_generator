"""The worker block: GPU detection made JSON-safe, cached, re-checked; CPU/RAM guards; credit text when known."""

from __future__ import annotations

import os
import sys
import threading
import time
import types

import pytest
from loguru import logger

from media_preview_generator.nodes.server import info, nodelog


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

    def test_a_detection_that_raises_reports_no_gpus_and_its_error(self, tmp_path):
        def detect():
            raise OSError("vulkaninfo: device lost")

        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=detect)

        block = worker.block(busy=0)

        assert block["gpus"] == [] and block["gpu_checked_at"] is not None
        assert block["gpu_error"] == "OSError: vulkaninfo: device lost"

    def test_a_detection_that_raises_keeps_the_last_gpu_list(self, tmp_path):
        results = [[("NVIDIA", "cuda:0", {"name": "GPU 0"})], RuntimeError("GPU busy")]

        def detect():
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=detect)
        worker.rescan()

        assert [g["device"] for g in worker.rescan()] == ["cuda:0"]

        block = worker.block(busy=0)
        assert [g["device"] for g in block["gpus"]] == ["cuda:0"]
        assert block["gpu_error"] == "RuntimeError: GPU busy" and worker.gpu_error == block["gpu_error"]

    def test_a_working_detection_clears_the_error(self, tmp_path):
        results = [OSError("device lost"), [("NVIDIA", "cuda:0", {"name": "GPU 0"})]]

        def detect():
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=detect)
        worker.rescan()

        worker.rescan()

        block = worker.block(busy=0)
        assert [g["device"] for g in block["gpus"]] == ["cuda:0"] and block["gpu_error"] is None

    def test_the_cached_answer_doesnt_wait_for_a_recheck(self, tmp_path):
        recheck_started, release = threading.Event(), threading.Event()
        calls = []

        def detect():
            calls.append(1)
            if len(calls) > 1:
                recheck_started.set()
                release.wait(10)  # a slow re-check: a busy or wedged GPU's FFmpeg test
            return [("NVIDIA", "cuda:0", {"name": "GPU 0"})]

        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=detect)
        worker.rescan()
        recheck = threading.Thread(target=worker.rescan, daemon=True)
        recheck.start()
        try:
            assert recheck_started.wait(5)
            answered = []
            reader = threading.Thread(target=lambda: answered.append(worker.block(busy=0)), daemon=True)
            reader.start()
            reader.join(2)

            assert answered and answered[0]["gpus"][0]["device"] == "cuda:0"
        finally:
            release.set()
            recheck.join(5)

    def test_a_background_recheck_logs_at_debug_unless_the_gpus_changed(self, tmp_path):
        # Every 5 minutes an idle node logged the same 3 lines per GPU (2 warnings per failed one) into its Log tab.
        found = [("NVIDIA", "cuda:0", {"name": "GPU 0"})]

        def detect():
            logger.info("Checking NVIDIA GPU 0 (GPU 0)...")
            logger.warning("card1: VAAPI test failed")
            return list(found)

        node_log = nodelog.install()
        levels: list[str] = []
        sink = logger.add(
            lambda message: levels.append(message.record["level"].name),
            level=0,
            filter=lambda r: r["message"] == "card1: VAAPI test failed" and not r["extra"].get(nodelog.HELD_EXTRA),
        )
        try:
            worker = info.WorkerInfo(temp_root=str(tmp_path), detect=detect)
            worker.rescan()  # the first pass, at start
            worker.rescan(quiet=True)  # a re-check: the same GPUs
            found.append(("INTEL", "/dev/dri/renderD128", {"name": "UHD 770"}))
            worker.rescan(quiet=True)  # a re-check that found a GPU more
        finally:
            logger.remove(sink)

        assert levels == ["WARNING", "DEBUG", "WARNING"]
        shown = [line["message"] for line in node_log.lines(100)]
        assert shown.count("Checking NVIDIA GPU 0 (GPU 0)...") == 2 and shown.count("card1: VAAPI test failed") == 2

    def test_the_loop_rechecks_quietly_after_its_first_pass(self, tmp_path, monkeypatch):
        passes = []
        worker = info.WorkerInfo(temp_root=str(tmp_path), recheck_s=3600, detect=list)
        monkeypatch.setattr(worker, "rescan", lambda quiet=False: passes.append(quiet) or [])
        worker.start()
        try:
            _until(lambda: len(passes) == 1)
            worker.note_gpu_error()
            _until(lambda: len(passes) == 2)
        finally:
            worker.stop()

        assert passes == [False, True]

    def test_info_during_the_first_detection_waits_for_it_instead_of_detecting_again(self, tmp_path):
        # A second detection right after the first doubled the first /v1/info's time and logged every GPU twice.
        calls = []
        first_started = threading.Event()

        def detect():
            calls.append(1)
            first_started.set()
            time.sleep(0.5)
            return [("NVIDIA", "cuda:0", {"name": "GPU 0"})]

        worker = info.WorkerInfo(temp_root=str(tmp_path), recheck_s=3600, detect=detect)
        worker.start()
        try:
            assert first_started.wait(5)

            block = worker.block(busy=0)
        finally:
            worker.stop()

        assert [g["device"] for g in block["gpus"]] == ["cuda:0"]
        assert len(calls) == 1

    def test_info_detects_itself_when_the_loop_ended_without_an_answer(self, tmp_path):
        calls = []
        worker = info.WorkerInfo(
            temp_root=str(tmp_path), detect=lambda: calls.append(1) or [("NVIDIA", "cuda:0", {"name": "GPU 0"})]
        )
        # A loop thread that ended before its first pass stored anything: waiting for it would wait forever.
        worker._thread = threading.Thread(target=lambda: None)
        worker._thread.start()
        worker._thread.join(5)

        assert [g["device"] for g in worker.gpus()] == ["cuda:0"] and len(calls) == 1

    def test_entries_are_json_safe(self):
        entry = info._gpu_entry(("AMD", "/dev/dri/renderD128", {"status": "failed", "error": ValueError("x")}))

        assert entry == {"type": "AMD", "device": "/dev/dri/renderD128", "name": "AMD", "status": "failed",
                         "error": "x"}  # fmt: skip

    def test_an_nvidia_card_that_failed_its_test_is_the_same_device_as_when_it_works(self):
        # Detection names a working card cuda:0 and the same card cuda once its CUDA test fails.
        failed = info._gpu_entry(("NVIDIA", "cuda", {"name": "Quadro P5000", "status": "failed", "error": "test"}))
        working = info._gpu_entry(("NVIDIA", "cuda:0", {"name": "Quadro P5000", "status": "ok"}))

        assert failed["device"] == working["device"] == "cuda:0"
        assert info._gpu_entry(("NVIDIA", "cuda:1", {"status": "ok"}))["device"] == "cuda:1"


class TestHost:
    def test_meminfo_read_in_bytes(self, tmp_path):
        path = tmp_path / "meminfo"
        path.write_text("MemTotal:       16000 kB\nMemFree: 1 kB\nMemAvailable:    8000 kB\n")

        assert info._meminfo(str(path)) == (16000 * 1024, 8000 * 1024)

    def test_meminfo_missing_is_none(self, tmp_path):
        assert info._meminfo(str(tmp_path / "nope")) == (None, None)

    def test_cores_are_the_ones_the_node_may_use_not_the_hosts(self, tmp_path, monkeypatch):
        # A node started with --cpus 0.5 (or pinned to one core) on a 20-core host said it had 20.
        monkeypatch.setattr(info.os, "sched_getaffinity", lambda pid: {0}, raising=False)
        monkeypatch.setattr(info.os, "cpu_count", lambda: 20)
        worker = info.WorkerInfo(temp_root=str(tmp_path), detect=list)

        assert worker.block(busy=0)["cpu"]["cores"] == 1

    @pytest.mark.parametrize(
        ("cpu_max", "cores"),
        [("50000 100000\n", 1), ("200000 100000\n", 2), ("250000 100000\n", 3), ("max 100000\n", 8), ("junk", 8)],
    )
    def test_cores_are_capped_by_the_containers_cpu_limit(self, tmp_path, monkeypatch, cpu_max, cores):
        path = tmp_path / "cpu.max"
        path.write_text(cpu_max)
        monkeypatch.setattr(info.os, "sched_getaffinity", lambda pid: set(range(8)), raising=False)

        assert info._cores(str(path)) == cores

    def test_cores_without_an_affinity_call_are_the_cpu_count(self, tmp_path, monkeypatch):
        monkeypatch.delattr(info.os, "sched_getaffinity", raising=False)  # macOS and Windows
        monkeypatch.setattr(info.os, "cpu_count", lambda: 6)

        assert info._cores(str(tmp_path / "no-cgroup")) == 6

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


class TestTempFolderInTheNodesLog:
    """The app holds a node whose temp folder can't be used; the node's own log, read first when setting one up, says
    so when that starts and when it ends."""

    @pytest.fixture
    def lines(self):
        seen: list[str] = []
        sink = logger.add(lambda m: seen.append(f"{m.record['level'].name} {m.record['message']}"), level="INFO")
        yield seen
        logger.remove(sink)

    @staticmethod
    def _worker(tmp_path, monkeypatch, problems: list[str | None]) -> info.WorkerInfo:
        answers = iter(problems)
        monkeypatch.setattr(info, "probe_temp_folder", lambda root: next(answers))
        monkeypatch.setattr(info, "_ffmpeg", lambda: {"path": "/usr/bin/ffmpeg", "version": "ffmpeg version 7.1"})
        return info.WorkerInfo(temp_root=str(tmp_path), recheck_s=3600, detect=list)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a 0500 folder")
    def test_a_temp_folder_the_node_cant_write_is_named_at_start(self, tmp_path, lines):
        temp_root = tmp_path / "scratch"
        temp_root.mkdir()
        temp_root.chmod(0o500)
        worker = info.WorkerInfo(temp_root=str(temp_root), recheck_s=3600, detect=list)
        try:
            worker.start()
        finally:
            worker.stop()
            temp_root.chmod(0o700)

        assert [line for line in lines if line.startswith("WARNING")] == [
            f"WARNING The node can't write to its temp folder {temp_root} (Permission denied): the app sends this "
            "node no files until that's fixed"
        ]

    def test_a_usable_temp_folder_logs_nothing(self, tmp_path, monkeypatch, lines):
        worker = self._worker(tmp_path, monkeypatch, [None, None])
        worker.start()
        worker.block(busy=0)
        worker.stop()

        assert not [line for line in lines if "temp folder" in line], lines

    def test_a_problem_is_logged_once_while_it_lasts_again_when_it_changes_and_when_it_ends(
        self, tmp_path, monkeypatch, lines
    ):
        full, read_only = f"The node's temp folder {tmp_path} is full", f"The node can't write to {tmp_path}"
        worker = self._worker(tmp_path, monkeypatch, [None, full, full, full, read_only, None, None])
        worker.start()
        for _ in range(6):  # every /v1/info checks the folder; the app asks every few seconds
            worker.block(busy=0)
        worker.stop()

        assert [line for line in lines if "temp folder" in line or "can't write" in line] == [
            f"WARNING {full}: the app sends this node no files until that's fixed",
            f"WARNING {read_only}: the app sends this node no files until that's fixed",
            f"INFO The node's temp folder {tmp_path} can be used again",
        ]


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
