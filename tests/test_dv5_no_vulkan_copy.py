"""Without hardware Vulkan, Dolby Vision Profile 5 thumbnails have the wrong colours.

The DV5 path no longer runs libplacebo on a software rasteriser (which
painted a green overlay); it extracts frames with no tone mapping. A real
Profile 5 clip run that way in the shipped image came out at normal
brightness with a green and purple tint, not dim as this copy used to say.
Every user-facing surface that describes the no-Vulkan state must say the
colours are wrong, and must not call the thumbnails dim or blame a green
overlay.  Each surface is its own cell — they are independent copies that
drifted together.  The behaviour itself is pinned in
``test_media_processing.py``
(``test_generate_images_dv_profile5_software_vulkan_uses_dv_safe_filter``).
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.gpu import _reset_vulkan_device_cache
from media_preview_generator.web.routes._helpers import _gpu_cache

SOFTWARE_DEVICE = "llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)"
DIM = re.compile(r"\bdim(?:mer|ly|ness)?\b", re.IGNORECASE)


def _assert_describes_wrong_colours(text: str) -> None:
    lowered = text.lower()
    assert not DIM.search(text), f"still says the thumbnails come out dim: {text!r}"
    assert "green overlay" not in lowered, f"still describes a green overlay: {text!r}"
    assert "wrong colours" in lowered or "green and purple" in lowered, f"doesn't say the colours are wrong: {text!r}"


def _capture_logs(level: str = "DEBUG") -> tuple[list[str], int]:
    messages: list[str] = []
    sink_id = logger.add(lambda msg: messages.append(str(msg)), level=level, format="{message}")
    return messages, sink_id


@pytest.fixture(autouse=True)
def _fresh_vulkan_cache():
    from media_preview_generator.web.routes import api_vulkan

    api_vulkan._loader_rejected_logged = False
    _reset_vulkan_device_cache()
    _gpu_cache["result"] = []
    yield
    _reset_vulkan_device_cache()
    _gpu_cache["result"] = None


def _container_gpus(monkeypatch: pytest.MonkeyPatch, tmp_path, *, render_node: bool, nvidia: bool) -> None:
    """Give dv5_no_vulkan_fix a container with (or without) a render node and NVIDIA's control node."""
    from media_preview_generator.gpu import vulkan_probe

    dri = tmp_path / "dri"
    dri.mkdir(exist_ok=True)
    if render_node:
        (dri / "renderD128").touch()
    nvidiactl = tmp_path / "nvidiactl"
    if nvidia:
        nvidiactl.touch()
    monkeypatch.setattr(vulkan_probe, "_RENDER_NODES_GLOB", str(dri / "renderD*"))
    monkeypatch.setattr(vulkan_probe, "_NVIDIA_CONTROL_NODE", str(nvidiactl))


@pytest.fixture
def no_gpu_in_the_container(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _container_gpus(monkeypatch, tmp_path, render_node=False, nvidia=False)


class TestTheFixNamesOnlyTheGpusTheContainerHas:
    @pytest.mark.parametrize(
        ("render_node", "nvidia", "says_nvidia", "says_dev_dri"),
        [
            pytest.param(False, False, True, True, id="no-gpu-at-all"),
            pytest.param(True, True, True, False, id="nvidia"),
            # Issue #408: an Intel GPU that is there and still has no Vulkan (a host kernel too old) gets no step;
            # the per-GPU check says why.
            pytest.param(True, False, False, False, id="intel-or-amd-only"),
            pytest.param(False, True, True, True, id="nvidia-without-a-render-node"),
        ],
    )
    def test_each_step_is_given_only_for_a_gpu_that_needs_it(
        self, render_node, nvidia, says_nvidia, says_dev_dri, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        from media_preview_generator.gpu.vulkan_probe import dv5_no_vulkan_fix

        monkeypatch.delenv("NVIDIA_DRIVER_CAPABILITIES", raising=False)
        _container_gpus(monkeypatch, tmp_path, render_node=render_node, nvidia=nvidia)

        fix = dv5_no_vulkan_fix()

        assert ("NVIDIA_DRIVER_CAPABILITIES=all" in fix) is says_nvidia, fix
        assert ("pass /dev/dri into the container" in fix) is says_dev_dri, fix
        assert fix == "" or (fix.startswith("To fix: ") and fix.endswith(".")), fix

    @pytest.mark.parametrize("device", [None, SOFTWARE_DEVICE], ids=["no-vulkan", "software"])
    def test_an_intel_gpu_without_vulkan_is_sent_to_the_per_gpu_check_not_told_to_pass_dev_dri(
        self, device, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.delenv("NVIDIA_DRIVER_CAPABILITIES", raising=False)
        _container_gpus(monkeypatch, tmp_path, render_node=True, nvidia=False)

        hit = _first_probe_line(device, "INFO" if device is None else "WARNING")

        _assert_describes_wrong_colours(hit)
        assert "/dev/dri" not in hit and "NVIDIA" not in hit, hit
        assert "  " not in hit, hit
        if device is not None:
            assert "GPU device tuning in the dashboard says why each GPU can't use Vulkan" in hit, hit


@pytest.mark.usefixtures("no_gpu_in_the_container")
class TestProbeLogs:
    def test_launch_failure_warning_says_wrong_colours_when_ffmpeg_missing(self) -> None:
        from media_preview_generator.gpu.vulkan_probe import _run_vulkan_probe

        messages, sink_id = _capture_logs("WARNING")
        try:
            with (
                patch("media_preview_generator.gpu.vulkan_probe._is_hwaccel_available", return_value=True),
                patch(
                    "media_preview_generator.gpu.vulkan_probe.subprocess.run", side_effect=FileNotFoundError("ffmpeg")
                ),
            ):
                _run_vulkan_probe()
        finally:
            logger.remove(sink_id)

        assert len(messages) == 1, messages
        _assert_describes_wrong_colours(messages[0])

    def test_unexpected_error_warning_says_wrong_colours_when_probe_raises(self) -> None:
        from media_preview_generator.gpu.vulkan_probe import _run_vulkan_probe

        messages, sink_id = _capture_logs("WARNING")
        try:
            with (
                patch("media_preview_generator.gpu.vulkan_probe._is_hwaccel_available", return_value=True),
                patch("media_preview_generator.gpu.vulkan_probe.subprocess.run", side_effect=RuntimeError("boom")),
            ):
                _run_vulkan_probe()
        finally:
            logger.remove(sink_id)

        assert len(messages) == 1, messages
        _assert_describes_wrong_colours(messages[0])

    def test_debug_line_says_wrong_colours_when_ffmpeg_lacks_vulkan(self) -> None:
        from media_preview_generator.gpu.vulkan_probe import _run_vulkan_probe

        messages, sink_id = _capture_logs("DEBUG")
        try:
            with patch("media_preview_generator.gpu.vulkan_probe._is_hwaccel_available", return_value=False):
                _run_vulkan_probe()
        finally:
            logger.remove(sink_id)

        hits = [m for m in messages if "without Vulkan" in m]
        assert len(hits) == 1, messages
        _assert_describes_wrong_colours(hits[0])

    def test_all_strategies_failed_warning_names_the_fix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NVIDIA_DRIVER_CAPABILITIES", raising=False)
        hit = _all_strategies_failed_line()

        _assert_describes_wrong_colours(hit)
        assert "NVIDIA_DRIVER_CAPABILITIES=all" in hit

    @pytest.mark.parametrize(
        ("device", "level"),
        [pytest.param(None, "INFO", id="no-vulkan"), pytest.param(SOFTWARE_DEVICE, "WARNING", id="software")],
    )
    def test_first_probe_outcome_says_wrong_colours_and_names_the_fix(
        self, device: str | None, level: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NVIDIA_DRIVER_CAPABILITIES", raising=False)
        hit = _first_probe_line(device, level)

        _assert_describes_wrong_colours(hit)
        assert "NVIDIA_DRIVER_CAPABILITIES=all" in hit
        assert "hardware Vulkan driver" in hit

    @pytest.mark.parametrize(
        ("caps", "already_asked"),
        [
            pytest.param(None, False, id="caps-unset"),
            pytest.param("compute,video,utility", False, id="caps-without-graphics"),
            pytest.param("all", True, id="caps-all"),
            pytest.param("compute,video,utility,graphics", True, id="caps-with-graphics"),
        ],
    )
    @pytest.mark.parametrize("surface", ["all-strategies-failed", "no-vulkan", "software"])
    def test_the_fix_doesnt_ask_for_a_capability_the_container_already_has(
        self, surface: str, caps: str | None, already_asked: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if caps is None:
            monkeypatch.delenv("NVIDIA_DRIVER_CAPABILITIES", raising=False)
        else:
            monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", caps)

        if surface == "all-strategies-failed":
            hit = _all_strategies_failed_line()
        elif surface == "no-vulkan":
            hit = _first_probe_line(None, "INFO")
        else:
            hit = _first_probe_line(SOFTWARE_DEVICE, "WARNING")

        assert ("set NVIDIA_DRIVER_CAPABILITIES=all" in hit) is not already_asked, hit
        assert (RUNTIME_FIX in hit) is already_asked, hit
        assert "on Intel/AMD, pass /dev/dri into the container" in hit


RUNTIME_FIX = (
    "on NVIDIA, NVIDIA_DRIVER_CAPABILITIES already asks for the Vulkan driver, so also start the container "
    "with --runtime=nvidia"
)


def _all_strategies_failed_line() -> str:
    from media_preview_generator.gpu.vulkan_probe import _probe_vulkan_device

    software = MagicMock(returncode=0, stderr=f"[Vulkan @ 0x1] Device 0 selected: {SOFTWARE_DEVICE}\n")
    messages, sink_id = _capture_logs("WARNING")
    try:
        with (
            patch("media_preview_generator.gpu.vulkan_probe._is_hwaccel_available", return_value=True),
            patch("media_preview_generator.gpu.vulkan_probe.subprocess.run", return_value=software),
            patch("media_preview_generator.gpu.vulkan_probe._find_nvidia_icd_json", return_value=None),
            patch("media_preview_generator.gpu.vulkan_probe._find_nvidia_egl_vendor_json", return_value=None),
            patch("media_preview_generator.gpu.vulkan_probe._find_libegl_nvidia", return_value=None),
        ):
            _probe_vulkan_device()
    finally:
        logger.remove(sink_id)

    hits = [m for m in messages if "couldn't be detected" in m]
    assert len(hits) == 1, messages
    return hits[0]


def _first_probe_line(device: str | None, level: str) -> str:
    from media_preview_generator.gpu.vulkan_probe import get_vulkan_device_info

    messages, sink_id = _capture_logs(level)
    try:
        with patch("media_preview_generator.gpu.vulkan_probe._probe_vulkan_device", return_value=device):
            get_vulkan_device_info()
    finally:
        logger.remove(sink_id)

    hits = [m for m in messages if "Dolby Vision" in m]
    assert len(hits) == 1, messages
    return hits[0]


def _diag(*, has_graphics: bool = True, icd_path: str | None = "/etc/vulkan/icd.d/nvidia_icd.json", glvkspirv=True):
    return {
        "nvidia_capabilities": "all" if has_graphics else "compute,video,utility",
        "nvidia_capabilities_has_graphics": has_graphics,
        "nvidia_icd_json_path": icd_path,
        "libnvidia_glvkspirv_found": glvkspirv,
        "libegl_nvidia_found": True,
        "nvidia_egl_vendor_json_path": None,
        "nvidia_drm_loaded": True,
        "nvidia_driver_version": "580.0.0",
    }


NVIDIA = {"type": "NVIDIA", "device": "/dev/nvidia0", "name": "NVIDIA GeForce RTX 3080"}
INTEL = {"type": "INTEL", "device": "/dev/dri/renderD128", "name": "Intel UHD 770"}

# Every branch of the warning builder — each renders its own body.
WARNING_CASES = [
    pytest.param([NVIDIA], [], _diag(has_graphics=False), id="A1-no-graphics-capability"),
    pytest.param([NVIDIA], [], _diag(icd_path=None), id="A2-icd-missing"),
    pytest.param([NVIDIA], [], _diag(glvkspirv=False), id="A3-glvkspirv-missing"),
    pytest.param([NVIDIA], [], _diag(), id="A4-loader-rejected"),
    pytest.param([NVIDIA, INTEL], [], None, id="B-nvidia-plus-mesa-no-dri"),
    pytest.param([INTEL], [], None, id="C-mesa-no-dri"),
    pytest.param([INTEL], ["/dev/dri/renderD128"], None, id="D-mesa-with-dri"),
    pytest.param([], [], None, id="E-no-gpu"),
]


class TestDashboardWarning:
    @pytest.mark.parametrize(("gpus", "render_nodes", "diag"), WARNING_CASES)
    def test_warning_says_wrong_colours_when_vulkan_is_software(self, gpus, render_nodes, diag) -> None:
        from media_preview_generator.web.routes.api_vulkan import _get_vulkan_info

        _gpu_cache["result"] = gpus
        with (
            patch("media_preview_generator.gpu.vulkan_probe._probe_vulkan_device", return_value=SOFTWARE_DEVICE),
            patch("media_preview_generator.web.routes.api_vulkan.glob.glob", return_value=render_nodes),
            patch("media_preview_generator.web.routes.api_vulkan._diagnose_vulkan_environment", return_value=diag),
        ):
            warning = _get_vulkan_info()["warning"]

        _assert_describes_wrong_colours(warning)
        assert "software rendering, which" not in warning

    def test_loader_rejected_log_says_wrong_colours_when_nvidia_checks_pass(self) -> None:
        from media_preview_generator.web.routes.api_vulkan import _get_vulkan_info

        _gpu_cache["result"] = [NVIDIA]
        messages, sink_id = _capture_logs("WARNING")
        try:
            with (
                patch("media_preview_generator.gpu.vulkan_probe._probe_vulkan_device", return_value=SOFTWARE_DEVICE),
                patch("media_preview_generator.web.routes.api_vulkan.glob.glob", return_value=[]),
                patch(
                    "media_preview_generator.web.routes.api_vulkan._diagnose_vulkan_environment", return_value=_diag()
                ),
            ):
                _get_vulkan_info()
                _get_vulkan_info()  # routine poll: must not log again
        finally:
            logger.remove(sink_id)

        hits = [m for m in messages if "loader still rejected" in m]
        assert len(hits) == 1, messages
        _assert_describes_wrong_colours(hits[0])

    def test_notification_title_says_wrong_colours_when_vulkan_is_software(self) -> None:
        from media_preview_generator.web.notifications import _build_vulkan_software_fallback_notification

        with (
            patch("media_preview_generator.gpu.vulkan_probe._probe_vulkan_device", return_value=SOFTWARE_DEVICE),
            patch("media_preview_generator.web.routes.api_vulkan.glob.glob", return_value=[]),
        ):
            notification = _build_vulkan_software_fallback_notification()

        assert notification is not None
        _assert_describes_wrong_colours(notification["title"])

    def test_debug_bundle_intro_says_wrong_colours(self, client, auth_headers) -> None:
        with (
            patch("media_preview_generator.gpu.vulkan_probe._probe_vulkan_device", return_value=SOFTWARE_DEVICE),
            patch("media_preview_generator.web.routes.api_vulkan._diagnose_vulkan_environment", return_value=_diag()),
        ):
            response = client.get("/api/system/vulkan/debug", headers=auth_headers)

        body = response.get_data(as_text=True)
        assert response.status_code == 200
        intro = body.split("--- Probe result ---", 1)[0]
        _assert_describes_wrong_colours(intro)
