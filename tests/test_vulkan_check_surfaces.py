"""Where the Vulkan check shows up (issue #408): the bell card, the GPU list API, the diagnostic bundle."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_preview_generator.gpu import _reset_vulkan_device_cache
from media_preview_generator.gpu import vulkan_check as vc
from media_preview_generator.gpu.vulkan_probe import VulkanProbeResult
from media_preview_generator.gpu.vulkan_probe import _probe_vulkan_device as real_probe_vulkan_device
from media_preview_generator.web.routes._helpers import _gpu_cache

SOFTWARE_DEVICE = "llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)"
SOFTWARE = VulkanProbeResult(device=SOFTWARE_DEVICE, is_software=True)
INTEL = {"type": "INTEL", "name": "Intel UHD Graphics 600", "device": "/dev/dri/renderD128", "status": "ok"}
AMD = {"type": "AMD", "name": "AMD Radeon RX 6600", "device": "/dev/dri/renderD129", "status": "ok"}
NVIDIA = {"type": "NVIDIA", "name": "RTX 3060", "device": "cuda:0", "status": "ok"}
APPLE = {"type": "APPLE", "name": "Apple M2", "device": "videotoolbox", "status": "ok"}

OLD_KERNEL = vc.VulkanCheck(
    False,
    "kernel_too_old",
    "The host's Linux kernel is too old",
    "The host runs Linux 4.4.302+. Intel's Vulkan driver needs softpin (Linux 4.5 or newer).",
    "Nothing in the container can add it. A host OS update with a newer kernel fixes it; until then credits are "
    "read on the CPU.",
)
NO_NODE = vc.VulkanCheck(
    False,
    "no_render_node",
    "/dev/dri isn't passed into the container",
    "/dev/dri/renderD129 doesn't exist inside the container.",
    "Add --device /dev/dri:/dev/dri, then restart.",
)
NVIDIA_NO_CAPS = vc.VulkanCheck(False, "nvidia_no_graphics_cap", "No NVIDIA Vulkan driver", "why", "fix")
READY = vc.VulkanCheck(True, "ready", "Vulkan ready", "Intel(R) Graphics", "")
DIAG = {
    "nvidia_capabilities": "compute,video,utility",
    "nvidia_capabilities_has_graphics": False,
    "nvidia_icd_json_path": None,
    "libnvidia_glvkspirv_found": False,
    "libegl_nvidia_found": False,
    "nvidia_egl_vendor_json_path": None,
    "nvidia_drm_loaded": True,
    "nvidia_driver_version": "570.1",
}


@pytest.fixture(autouse=True)
def _software_vulkan():
    from media_preview_generator.web.routes import api_vulkan

    api_vulkan._loader_rejected_logged = False
    _reset_vulkan_device_cache()
    with (
        patch("media_preview_generator.gpu.vulkan_probe._probe_vulkan_device", return_value=SOFTWARE_DEVICE),
        patch("media_preview_generator.web.routes.api_vulkan.glob.glob", return_value=[]),
        patch("media_preview_generator.web.routes.api_vulkan._diagnose_vulkan_environment", return_value=DIAG),
    ):
        yield
    _reset_vulkan_device_cache()
    _gpu_cache["result"] = None


def _card(monkeypatch, gpus, checks):
    from media_preview_generator.web.notifications import _build_vulkan_software_fallback_notification

    _gpu_cache["result"] = gpus
    monkeypatch.setattr(vc, "get_vulkan_checks", lambda g, diag=None: checks)
    return _build_vulkan_software_fallback_notification()


class TestBellCard:
    def test_one_gpu_names_the_gpu_and_the_cause_with_the_approved_sections(self, monkeypatch):
        card = _card(monkeypatch, [INTEL], [OLD_KERNEL])
        assert card["title"] == "Intel UHD Graphics 600 can't use Vulkan: the host's Linux kernel is too old"
        body = card["body_html"]
        assert body.index("What this means") < body.index("<strong>Why</strong>") < body.index("What to do")
        assert "Credits text is read on the CPU: just as accurate, slower." in body
        assert "Dolby Vision Profile 5 thumbnails get a green and purple tint." in body
        assert "Previews and intros still use the GPU." in body
        assert "Intel&#39;s Vulkan driver needs softpin (Linux 4.5 or newer)." in body
        assert "Nothing, if the CPU keeps up. A host OS update with a newer kernel fixes it." in body
        assert "Nothing in the container can add it" not in body

    def test_keeps_the_id_source_and_the_diagnostic_bundle_button_hook(self, monkeypatch):
        card = _card(monkeypatch, [INTEL], [OLD_KERNEL])
        assert (card["id"], card["source"], card["severity"]) == ("vulkan_software_fallback", "vulkan_probe", "warning")
        assert card["device"] == SOFTWARE_DEVICE
        assert card["action"] == {"href": "/settings#section-processing", "label": "Open GPU check"}

    def test_the_fix_is_what_to_do_for_other_causes(self, monkeypatch):
        card = _card(monkeypatch, [AMD], [NO_NODE])
        assert card["title"] == "AMD Radeon RX 6600 can't use Vulkan: /dev/dri isn't passed into the container"
        assert "Add --device /dev/dri:/dev/dri, then restart." in card["body_html"]

    def test_several_gpus_say_how_many_and_label_each_reason(self, monkeypatch):
        card = _card(monkeypatch, [INTEL, AMD], [OLD_KERNEL, NO_NODE])
        assert card["title"] == "Vulkan isn't working on 2 GPUs"
        assert "<strong>Intel UHD Graphics 600:</strong>" in card["body_html"]
        assert "<strong>AMD Radeon RX 6600:</strong>" in card["body_html"]

    def test_only_failing_gpus_count(self, monkeypatch):
        card = _card(monkeypatch, [INTEL, AMD], [READY, NO_NODE])
        assert card["title"].startswith("AMD Radeon RX 6600 can't use Vulkan")

    def test_credits_bullet_only_for_webgpu_vendors(self, monkeypatch):
        card = _card(monkeypatch, [APPLE], [NO_NODE])
        assert "Credits text" not in card["body_html"]
        assert "green and purple tint" in card["body_html"]

    def test_nvidia_keeps_its_detailed_html(self, monkeypatch):
        card = _card(monkeypatch, [NVIDIA], [NVIDIA_NO_CAPS])
        assert card["title"] == "RTX 3060 can't use Vulkan: no NVIDIA Vulkan driver"
        assert "NVIDIA_DRIVER_CAPABILITIES=compute,video,utility" in card["body_html"]
        assert "What to do" not in card["body_html"]

    def test_gpu_name_and_text_are_escaped(self, monkeypatch):
        evil = dict(INTEL, name="<img src=x onerror=alert(1)>")
        check = vc.VulkanCheck(False, "driver_refused", "T", "<script>x</script>", "<b>fix</b>")
        body = _card(monkeypatch, [evil, AMD], [check, check])["body_html"]
        assert "<script>" not in body and "<img" not in body and "<b>fix" not in body
        assert "&lt;script&gt;" in body

    def test_without_a_known_failure_the_general_dolby_vision_card_stays(self, monkeypatch):
        card = _card(monkeypatch, [APPLE], [None])
        assert card["title"] == "Dolby Vision Profile 5 thumbnails will have the wrong colours"
        assert "action" not in card

    def test_no_card_when_vulkan_is_fine(self, monkeypatch):
        from media_preview_generator.web.notifications import _build_vulkan_software_fallback_notification

        with patch("media_preview_generator.gpu.vulkan_probe._probe_vulkan_device", return_value="Intel(R) Graphics"):
            _reset_vulkan_device_cache()
            assert _build_vulkan_software_fallback_notification() is None


class TestGpuListApi:
    def test_status_adds_vulkan_to_each_gpu_and_leaves_the_cache_alone(self, client, auth_headers, monkeypatch):
        from media_preview_generator.web.routes import api_vulkan

        _gpu_cache["result"] = [INTEL, AMD]
        monkeypatch.setattr(api_vulkan, "get_vulkan_checks", lambda gpus, diag=None: [OLD_KERNEL, None])
        gpus = client.get("/api/system/status", headers=auth_headers).get_json()["gpus"]
        assert gpus[0]["vulkan"]["cause"] == "kernel_too_old"
        assert gpus[0]["vulkan"]["fix"] == OLD_KERNEL.fix
        assert gpus[1]["vulkan"] is None
        assert all("vulkan" not in gpu for gpu in _gpu_cache["result"])

    def test_rescan_clears_the_check_cache_and_includes_vulkan(self, client, auth_headers, monkeypatch):
        from media_preview_generator.web.routes import api_vulkan

        monkeypatch.setattr(api_vulkan, "get_vulkan_checks", lambda gpus, diag=None: [READY] * len(gpus))
        cleared = []
        monkeypatch.setattr(vc, "clear_vulkan_check_cache", lambda: cleared.append(1))
        with patch(
            "media_preview_generator.gpu.detect.detect_all_gpus",
            return_value=[("INTEL", "/dev/dri/renderD128", {"name": "Arc"})],
        ):
            gpus = client.post("/api/system/rescan-gpus", headers=auth_headers).get_json()["gpus"]
        assert cleared
        assert gpus[0]["vulkan"]["ok"] is True

    def test_a_failing_check_leaves_the_gpu_list_without_vulkan(self, client, auth_headers, monkeypatch):
        from media_preview_generator.web.routes import api_vulkan

        def boom(gpus, diag=None):
            raise RuntimeError("probe broke")

        _gpu_cache["result"] = [INTEL]
        monkeypatch.setattr(api_vulkan, "get_vulkan_checks", boom)
        response = client.get("/api/system/status", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json()["gpus"][0]["vulkan"] is None


class TestDiagnosticBundle:
    def test_has_the_host_check_and_credits_sections(self, client, auth_headers, monkeypatch):
        from media_preview_generator.web.routes import api_vulkan

        _gpu_cache["result"] = [INTEL]
        failing = vc.VulkanCheck(
            False,
            "kernel_too_old",
            "T",
            "why text",
            "fix",
            {"node": "/dev/dri/renderD128", "driver": "i915", "opened": True, "params": {"I915_PARAM_HAS_LLC": 1}},
        )
        monkeypatch.setattr(api_vulkan, "get_vulkan_checks", lambda gpus, diag=None: [failing])
        monkeypatch.setattr(
            "media_preview_generator.markers.credits.textdet_helper.credits_text_devices",
            lambda: [{"key": "/dev/dri/renderD128", "backend": "cpu", "reason": "Vulkan reports no hardware GPU"}],
        )
        response = client.get("/api/system/vulkan/debug", headers=auth_headers)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "--- Host ---" in body and "kernel release:" in body and "uid:" in body
        assert "--- Vulkan check per GPU ---" in body
        assert "cause: kernel_too_old" in body and "driver: i915" in body and "I915_PARAM_HAS_LLC: 1" in body
        assert "--- Credits text detection ---" in body
        assert "/dev/dri/renderD128: cpu (Vulkan reports no hardware GPU)" in body
        assert "a GPU that can't use Vulkan" in body.split("--- Probe result ---")[0]

    def test_a_gpu_with_no_check_says_so_and_an_idle_pool_says_nothing_ran(self, client, auth_headers, monkeypatch):
        monkeypatch.setattr("media_preview_generator.markers.credits.textdet_helper._pool", None)
        _gpu_cache["result"] = [APPLE]
        body = client.get("/api/system/vulkan/debug", headers=auth_headers).get_data(as_text=True)
        assert "(not checked" in body
        assert "  (no credits text detection has run yet)" in body

    def test_a_pool_with_a_device_lists_it(self, client, auth_headers, monkeypatch):
        _gpu_cache["result"] = [APPLE]
        monkeypatch.setattr(
            "media_preview_generator.markers.credits.textdet_helper.credits_text_devices",
            lambda: [{"key": "cuda:0", "backend": "webgpu", "reason": ""}],
        )
        body = client.get("/api/system/vulkan/debug", headers=auth_headers).get_data(as_text=True)
        assert "  cuda:0: webgpu" in body
        assert "no credits text detection has run yet" not in body

    def test_it_is_refused_without_a_login_once_setup_is_done(self, client):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("setup_complete", True)
        response = client.get("/api/system/vulkan/debug")
        assert response.status_code == 401
        assert "Authentication required" in response.get_data(as_text=True)


class TestProbeWarnings:
    def _warnings(self, run):
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
        try:
            run()
        finally:
            logger.remove(sink)
        return messages

    def test_software_warning_points_at_gpu_device_tuning_not_settings_system(self):
        from media_preview_generator.gpu import vulkan_probe

        # Its own probe answer and mode: the cached result and node mode are process-wide, so another test's leftovers
        # would decide the warning otherwise.
        _reset_vulkan_device_cache()
        with (
            patch.object(vulkan_probe, "_probe_vulkan_device", return_value=SOFTWARE_DEVICE),
            patch.object(vulkan_probe, "is_node_mode", return_value=False),
        ):
            messages = self._warnings(vulkan_probe.get_vulkan_device_info)
        _reset_vulkan_device_cache()
        assert len(messages) == 1
        assert "Settings → Processing Options → GPU device tuning" in messages[0]
        assert "Settings → System" not in messages[0]

    @pytest.mark.parametrize(("nvidia_files", "expected"), [(True, True), (False, False)])
    def test_strategy_3_only_claims_nvidia_fixes_when_an_nvidia_file_exists(self, nvidia_files, expected):
        from media_preview_generator.gpu import vulkan_probe

        found = "/x/nvidia.json" if nvidia_files else None
        with (
            patch.object(vulkan_probe, "_find_nvidia_icd_json", return_value=found),
            patch.object(vulkan_probe, "_find_nvidia_egl_vendor_json", return_value=None),
            patch.object(vulkan_probe, "_find_libegl_nvidia", return_value=None),
            patch.object(vulkan_probe, "_run_vulkan_probe", return_value=(SOFTWARE_DEVICE, "")),
            patch.object(vulkan_probe, "is_node_mode", return_value=False),
        ):
            messages = self._warnings(real_probe_vulkan_device)
        text = next(m for m in messages if "couldn't be detected" in m)
        assert ("NVIDIA-specific fixes" in text) is expected
        assert "Settings → Processing Options → GPU device tuning" in text
