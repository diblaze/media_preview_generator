"""Why a GPU can't use Vulkan (issue #408): every cause, the i915 feature table, hardware vs software probes."""

from __future__ import annotations

import errno
import fcntl
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from loguru import logger

from media_preview_generator.gpu import vulkan_check as vc
from media_preview_generator.gpu.vulkan_check import get_vulkan_checks as real_get_vulkan_checks
from media_preview_generator.gpu.vulkan_probe import VulkanProbeResult

SOFTWARE = VulkanProbeResult(device="llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)", is_software=True)
NOTHING = VulkanProbeResult(device=None, is_software=False)
INTEL = {"type": "INTEL", "name": "Intel UHD Graphics 600", "device": "/dev/dri/renderD128", "status": "ok"}
AMD = {"type": "AMD", "name": "AMD Radeon RX 6600", "device": "/dev/dri/renderD129", "status": "ok"}
NVIDIA = {"type": "NVIDIA", "name": "RTX 3060", "device": "cuda:0", "status": "ok"}
ALL_PRESENT = {name: 1 for name, *_ in vc.I915_REQUIRED_PARAMS} | {"I915_PARAM_HAS_LLC": 1}


def _diag(**overrides):
    base = {
        "nvidia_capabilities_has_graphics": True,
        "nvidia_icd_json_path": "/etc/vulkan/icd.d/nvidia_icd.json",
        "libnvidia_glvkspirv_found": True,
    }
    return base | overrides


@pytest.fixture
def host(monkeypatch):
    """A container where the render node opens, i915 drives it and the kernel has every feature."""
    state = SimpleNamespace(
        stat_error=None,
        open_error=None,
        driver="i915",
        params=dict(ALL_PRESENT),
        ioctl_calls=[],
        closed=[],
    )
    monkeypatch.setattr(
        vc.os,
        "stat",
        lambda node: state.stat_error and (_ for _ in ()).throw(state.stat_error) or SimpleNamespace(st_gid=44),
    )

    def fake_open(node, flags):
        if state.open_error:
            raise state.open_error
        return 99

    monkeypatch.setattr(vc.os, "open", fake_open)
    monkeypatch.setattr(vc.os, "close", lambda fd: state.closed.append(fd))
    monkeypatch.setattr(vc.os, "uname", lambda: SimpleNamespace(release="4.4.302+"))
    monkeypatch.setattr(vc.os, "getuid", lambda: 1000)
    monkeypatch.setattr(vc.os, "getgid", lambda: 100)
    monkeypatch.setattr(vc.os, "getgroups", lambda: [100, 5])
    monkeypatch.setattr(vc.os.path, "realpath", lambda path: f"/sys/bus/pci/drivers/{state.driver}")

    numbers = {number: name for name, number, *_ in vc.I915_REQUIRED_PARAMS} | {
        vc.I915_PARAM_HAS_LLC: "I915_PARAM_HAS_LLC"
    }

    def fake_ioctl(fd, request, arg):
        state.ioctl_calls.append((fd, request))
        value = state.params.get(numbers[arg.param])
        if value is None:
            raise OSError(errno.EINVAL, "invalid")
        arg.value[0] = value

    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)
    return state


def _only(check_list):
    assert len(check_list) == 1
    return check_list[0]


class TestRenderNodeCauses:
    def test_no_render_node_when_dev_dri_is_missing(self, host):
        host.stat_error = FileNotFoundError()
        check = _only(vc.check_gpus([INTEL], SOFTWARE))
        assert (check.ok, check.cause) == (False, "no_render_node")
        assert check.title == "/dev/dri isn't passed into the container"
        assert check.fix == "Add --device /dev/dri:/dev/dri, then restart."

    @pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
    def test_cant_open_names_the_group_and_the_user(self, host, code):
        host.open_error = OSError(code, os.strerror(code))
        check = _only(vc.check_gpus([INTEL], SOFTWARE))
        assert check.cause == "cant_open"
        assert check.title == "The app can't open /dev/dri/renderD128"
        assert check.fix == (
            "It belongs to group 44; the app runs as 1000:100 in groups 5, 100. "
            "Add --group-add 44 (compose: group_add: ['44']), then restart."
        )
        assert host.ioctl_calls == []

    def test_another_open_error_is_not_a_permission_problem(self, host):
        host.open_error = OSError(errno.EIO, "I/O error")
        assert _only(vc.check_gpus([INTEL], SOFTWARE)).cause == "driver_refused"

    def test_radeon_driver_is_an_old_amd_driver(self, host):
        host.driver = "radeon"
        check = _only(vc.check_gpus([AMD], SOFTWARE))
        assert check.cause == "old_amd_driver"
        assert check.title == "This AMD GPU uses the old radeon driver"
        assert check.fix == "AMD's Vulkan driver needs the amdgpu kernel driver (host setting)."

    def test_old_kernel_names_every_missing_feature_and_the_newest_linux_needed(self, host):
        host.params["I915_PARAM_HAS_EXEC_SOFTPIN"] = 0
        host.params["I915_PARAM_HAS_EXEC_FENCE_ARRAY"] = None
        check = _only(vc.check_gpus([INTEL], SOFTWARE))
        assert check.cause == "kernel_too_old"
        assert check.title == "The host's Linux kernel is too old"
        assert check.why == (
            "The host runs Linux 4.4.302+. Intel's Vulkan driver needs softpin, syncobj support (Linux 4.14 or newer)."
        )
        assert check.facts["missing"] == ["softpin", "syncobj support"]
        assert check.fix.startswith("Nothing in the container can add it.")

    def test_every_kernel_feature_is_required(self, host):
        for name, _number, label, since in vc.I915_REQUIRED_PARAMS:
            host.params = dict(ALL_PRESENT)
            host.params["I915_PARAM_HAS_LLC"] = 0  # makes MMAP_VERSION count too
            host.params[name] = 0
            check = _only(vc.check_gpus([INTEL], SOFTWARE))
            assert (check.cause, check.facts["missing"]) == ("kernel_too_old", [label]), name
            assert f"needs {label} (Linux {since} or newer)" in check.why

    def test_mmap_version_only_counts_without_llc(self, host):
        host.params["I915_PARAM_MMAP_VERSION"] = 0
        host.params["I915_PARAM_HAS_LLC"] = 1
        assert _only(vc.check_gpus([INTEL], SOFTWARE)).cause == "driver_refused"
        host.params["I915_PARAM_HAS_LLC"] = 0
        assert _only(vc.check_gpus([INTEL], SOFTWARE)).facts["missing"] == ["wc mmap"]

    def test_an_ioctl_error_counts_as_missing_like_mesa(self, host):
        host.params["I915_PARAM_HAS_EXECBUF2"] = None
        assert _only(vc.check_gpus([INTEL], SOFTWARE)).facts["missing"] == ["execbuf2"]

    def test_driver_refused_when_the_kernel_has_everything(self, host):
        check = _only(vc.check_gpus([INTEL], SOFTWARE))
        assert check.cause == "driver_refused"
        assert check.title == "The Intel Vulkan driver refused this GPU"
        assert check.fix == (
            "The host kernel has every feature the app checks for. Copy the diagnostic bundle into a GitHub issue."
        )
        assert check.facts["params"] == ALL_PRESENT

    @pytest.mark.parametrize("driver", ["amdgpu", "xe"])
    def test_driver_refused_without_an_i915_check_doesnt_vouch_for_the_kernel(self, host, driver):
        host.driver = driver
        check = _only(vc.check_gpus([AMD if driver == "amdgpu" else INTEL], SOFTWARE))
        assert check.cause == "driver_refused"
        assert check.fix == "Copy the diagnostic bundle into a GitHub issue."

    def test_driver_refused_for_amd_names_amd(self, host):
        host.driver = "amdgpu"
        assert _only(vc.check_gpus([AMD], SOFTWARE)).title == "The AMD Vulkan driver refused this GPU"

    def test_the_getparam_struct_matches_the_kernels_layout(self):
        # struct drm_i915_getparam { __s32 param; int __user *value; }: 4 bytes, padding, then an 8-byte pointer.
        import ctypes

        assert ctypes.sizeof(vc._GetParam) == 16
        assert vc._GetParam.value.offset == 8

    def test_driver_refused_quotes_the_loaders_failing_line(self, host):
        buffer = "ok line\nERROR: vkEnumeratePhysicalDevices failed with error code -3\nlater line"
        check = _only(vc.check_gpus([INTEL], SOFTWARE, debug_buffer=buffer))
        assert check.why == "The Vulkan loader reported: ERROR: vkEnumeratePhysicalDevices failed with error code -3."

    def test_a_specific_why_is_kept_ahead_of_the_loaders_line(self, host):
        host.open_error = OSError(errno.EIO, "Input/output error")
        buffer = "ERROR: vkEnumeratePhysicalDevices failed with error code -3"
        check = _only(vc.check_gpus([INTEL], SOFTWARE, debug_buffer=buffer))
        assert check.cause == "driver_refused"
        assert check.why.startswith("Opening /dev/dri/renderD128 failed: Input/output error.")
        assert check.why.endswith(
            "The Vulkan loader reported: ERROR: vkEnumeratePhysicalDevices failed with error code -3."
        )

    @pytest.mark.parametrize("driver", ["amdgpu", "nvidia", "xe"])
    def test_the_i915_ioctl_runs_only_on_an_i915_node(self, host, driver):
        host.driver = driver
        vc.check_gpus([INTEL], SOFTWARE)
        assert host.ioctl_calls == []

    def test_the_i915_ioctl_uses_the_getparam_request(self, host):
        vc.check_gpus([INTEL], SOFTWARE)
        assert {request for _fd, request in host.ioctl_calls} == {0xC0106446}
        assert len(host.ioctl_calls) == 6
        assert host.closed == [99]

    def test_nothing_is_opened_when_vulkan_has_no_hardware_issue_for_an_unknown_vendor(self, host):
        assert vc.check_gpus([{"type": "APPLE", "name": "M1", "device": "videotoolbox"}], SOFTWARE) == [None]


class TestOtherCauses:
    def test_ffmpeg_without_vulkan_wins_for_every_vendor(self, host):
        checks = vc.check_gpus([INTEL, AMD, NVIDIA], NOTHING, ffmpeg_has_vulkan=False)
        assert [c.cause for c in checks] == ["no_vulkan_in_ffmpeg"] * 3
        assert checks[0].title == "This FFmpeg has no Vulkan"
        assert host.ioctl_calls == []

    @pytest.mark.parametrize(
        ("diag", "cause"),
        [
            (_diag(nvidia_capabilities_has_graphics=False), "nvidia_no_graphics_cap"),
            (_diag(nvidia_icd_json_path=None), "nvidia_icd_missing"),
            (_diag(libnvidia_glvkspirv_found=False), "nvidia_spirv_missing"),
            (_diag(), "nvidia_loader_rejected"),
        ],
    )
    def test_nvidia_cases(self, diag, cause):
        assert vc.nvidia_cause(diag) == cause
        check = _only(vc.check_gpus([NVIDIA], SOFTWARE, nvidia_diag=lambda: diag))
        assert (check.ok, check.cause) == (False, cause)
        assert check.title and check.why and check.fix

    def test_nvidia_diag_is_read_only_for_nvidia_gpus(self, host):
        calls = []
        vc.check_gpus([INTEL], SOFTWARE, nvidia_diag=lambda: calls.append(1) or _diag())
        assert calls == []

    def test_a_cause_comes_from_software_or_nothing_alike(self, host):
        assert _only(vc.check_gpus([INTEL], NOTHING)).cause == "driver_refused"
        assert _only(vc.check_gpus([INTEL], SOFTWARE)).cause == "driver_refused"

    def test_to_dict_is_plain_data(self, host):
        data = _only(vc.check_gpus([INTEL], SOFTWARE)).to_dict()
        assert set(data) == {"ok", "cause", "title", "why", "fix"}


class TestHardwareProbe:
    @pytest.mark.parametrize(
        ("device", "ready_index"),
        [
            ("Intel(R) Graphics (RPL-S) (integrated) (0xa780)", 0),
            ("AMD Radeon RX 6600 (RADV NAVI23)", 1),
            ("NVIDIA GeForce RTX 3060 (discrete)", 2),
        ],
    )
    def test_only_the_gpu_of_the_probes_vendor_is_ready(self, host, device, ready_index):
        checks = vc.check_gpus([INTEL, AMD, NVIDIA], VulkanProbeResult(device=device, is_software=False))
        assert [c is not None for c in checks] == [i == ready_index for i in range(3)]
        ready = checks[ready_index]
        assert (ready.ok, ready.title, ready.why, ready.fix) == (True, "Vulkan ready", device, "")
        assert host.ioctl_calls == []


class TestCacheAndLogs:
    def test_checks_are_cached_per_probe_and_gpu_list_and_cleared_on_rescan(self, host, monkeypatch):
        probes = []
        monkeypatch.setattr(vc, "get_vulkan_device_info", lambda: probes.append(1) or SOFTWARE)
        monkeypatch.setattr(vc, "get_vulkan_debug_buffer", lambda: "")
        monkeypatch.setattr("media_preview_generator.gpu.ffmpeg_capabilities._is_hwaccel_available", lambda name: True)
        vc.clear_vulkan_check_cache()
        first = real_get_vulkan_checks([INTEL])
        assert real_get_vulkan_checks([INTEL]) is first
        assert real_get_vulkan_checks([INTEL, AMD]) is not first
        vc.clear_vulkan_check_cache()
        assert real_get_vulkan_checks([INTEL]) is not first

    def test_a_compute_that_began_before_a_clear_does_not_store(self, host, monkeypatch):
        monkeypatch.setattr(vc, "get_vulkan_device_info", lambda: SOFTWARE)
        monkeypatch.setattr(vc, "get_vulkan_debug_buffer", lambda: "")
        monkeypatch.setattr("media_preview_generator.gpu.ffmpeg_capabilities._is_hwaccel_available", lambda name: True)
        vc.clear_vulkan_check_cache()
        real_check_gpus = vc.check_gpus

        def check_then_clear(*args, **kwargs):
            result = real_check_gpus(*args, **kwargs)
            vc.clear_vulkan_check_cache()
            return result

        monkeypatch.setattr(vc, "check_gpus", check_then_clear)
        first = real_get_vulkan_checks([INTEL])
        assert real_get_vulkan_checks([INTEL]) is not first

    def test_two_callers_share_one_compute(self, host, monkeypatch):
        import threading

        monkeypatch.setattr(vc, "get_vulkan_device_info", lambda: SOFTWARE)
        monkeypatch.setattr(vc, "get_vulkan_debug_buffer", lambda: "")
        monkeypatch.setattr("media_preview_generator.gpu.ffmpeg_capabilities._is_hwaccel_available", lambda name: True)
        vc.clear_vulkan_check_cache()
        computing, release, calls = threading.Event(), threading.Event(), []

        def slow_check_gpus(*args, **kwargs):
            calls.append(1)
            computing.set()
            release.wait(5)
            return [None]

        monkeypatch.setattr(vc, "check_gpus", slow_check_gpus)
        results = []
        first = threading.Thread(target=lambda: results.append(real_get_vulkan_checks([INTEL])))
        second = threading.Thread(target=lambda: results.append(real_get_vulkan_checks([INTEL])))
        first.start()
        assert computing.wait(5)
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        assert len(calls) == 1
        assert results[0] is results[1]

    def test_startup_warning_has_the_approved_shape(self, host):
        host.driver = "radeon"
        check = _only(vc.check_gpus([AMD], SOFTWARE))
        assert vc.startup_warning(AMD, check) == (
            "Vulkan: AMD Radeon RX 6600 (/dev/dri/renderD129) can't use Vulkan: This AMD GPU uses the old radeon "
            "driver. /dev/dri/renderD129 is driven by the radeon kernel driver. Effect: credits text is read on the "
            "CPU (just as accurate, slower) and Dolby Vision Profile 5 thumbnails get a green and purple tint; "
            "previews still use the GPU. AMD's Vulkan driver needs the amdgpu kernel driver (host setting)."
        )

    def test_startup_warning_drops_the_credits_clause_outside_webgpu_vendors(self, host):
        check = vc.VulkanCheck(False, "x", "Title", "Why.", "Fix.")
        text = vc.startup_warning({"type": "APPLE", "name": "M1", "device": "videotoolbox"}, check)
        assert "credits" not in text
        assert "Effect: Dolby Vision Profile 5 thumbnails get a green and purple tint" in text

    def test_one_warning_per_failing_gpu_and_none_for_ready_or_unknown(self, host, monkeypatch):
        ready = vc.VulkanCheck(True, "ready", "Vulkan ready", "Intel", "")
        failing = vc.check_gpus([AMD], SOFTWARE)[0]
        monkeypatch.setattr(vc, "get_vulkan_checks", lambda gpus, diag=None: [ready, failing, None])
        messages = []
        sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
        try:
            vc.log_vulkan_warnings([INTEL, AMD, NVIDIA])
        finally:
            logger.remove(sink)
        assert len(messages) == 1
        assert messages[0].startswith("Vulkan: AMD Radeon RX 6600")

    def test_a_failing_check_never_raises_from_the_log(self, monkeypatch):
        def boom(gpus, diag=None):
            raise RuntimeError("no")

        monkeypatch.setattr(vc, "get_vulkan_checks", boom)
        vc.log_vulkan_warnings([INTEL])


class TestLoaderLine:
    def test_last_matching_line_or_none(self):
        assert vc.loader_failure_line("") is None
        assert vc.loader_failure_line("a\nfailed with error code -1\nfailed with error code -3\n") == (
            "failed with error code -3"
        )


@pytest.mark.parametrize("path", ["/sys/class/drm/renderD128/device/driver"])
def test_a_node_with_no_sysfs_driver_link_has_no_driver(path):
    with patch.object(vc.os.path, "realpath", side_effect=lambda p: p):
        assert vc.drm_driver_of("/dev/dri/renderD128") is None


def test_the_module_imports_where_there_is_no_fcntl():
    """The Windows node imports the GPU modules; fcntl exists only on POSIX, so the check may not need it at import."""
    import subprocess
    import sys

    code = "import sys; sys.modules['fcntl'] = None; import media_preview_generator.gpu.vulkan_check"
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
