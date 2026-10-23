"""Say why a GPU can't use Vulkan (issue #408).

Credits text detection and Dolby Vision Profile 5 tone mapping need a hardware Vulkan device. When the probe found only
a software renderer (or nothing), :func:`check_gpus` works out, per GPU, which of a few concrete causes applies and
what the user can do about it. Everything here is plain text, worked out from the GPU list, the probe result and a
read-only look at the container (render node, sysfs, env, ``/proc``); the web pages and the remote node escape and
render it.
"""

from __future__ import annotations

import ctypes
import errno
import glob
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .vulkan_probe import (
    NVIDIA_GRAPHICS_FILES_DOCS,
    VulkanProbeResult,
    _is_nvidia_vulkan_device,
    get_vulkan_debug_buffer,
    get_vulkan_device_info,
)

DRM_IOCTL_I915_GETPARAM = 0xC0106446
I915_PARAM_HAS_LLC = 17

# (param name, number, label, Linux version that added it). Labels match Mesa's "kernel missing ..." messages so a
# user can search for them. Mesa treats an ioctl error as "missing", and so does :func:`missing_i915_features`.
I915_REQUIRED_PARAMS = (
    ("I915_PARAM_HAS_WAIT_TIMEOUT", 19, "gem wait", "3.6"),
    ("I915_PARAM_HAS_EXECBUF2", 9, "execbuf2", "2.6.33"),
    ("I915_PARAM_MMAP_VERSION", 30, "wc mmap", "4.0"),
    ("I915_PARAM_HAS_EXEC_SOFTPIN", 37, "softpin", "4.5"),
    ("I915_PARAM_HAS_EXEC_FENCE_ARRAY", 49, "syncobj support", "4.14"),
)

# Standard locations the Vulkan loader searches for the NVIDIA ICD JSON.
# nvidia-container-toolkit mounts at /etc/vulkan/icd.d/; some loaders and
# distributions only scan /usr/share/vulkan/icd.d/. Both are checked so
# the Case A dispatch can tell "file exists somewhere" from "file missing
# everywhere" without needing to know which path a given loader prefers.
_NVIDIA_ICD_JSON_SEARCH_PATHS = (
    "/etc/vulkan/icd.d/nvidia_icd.json",
    "/usr/share/vulkan/icd.d/nvidia_icd.json",
)

# Glob patterns for libnvidia-glvkspirv.so, the sibling library the NVIDIA
# Vulkan ICD needs to `dlopen`. Checking multiple locations covers amd64
# Debian, arm64, and non-standard layouts.
_LIBNVIDIA_GLVKSPIRV_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libnvidia-glvkspirv.so*",
    "/usr/lib/aarch64-linux-gnu/libnvidia-glvkspirv.so*",
    "/usr/lib/libnvidia-glvkspirv.so*",
    "/usr/lib64/libnvidia-glvkspirv.so*",
)

# Glob patterns for libEGL_nvidia.so, the library GLVND routes to when
# NVIDIA's vendor config is present.  Strategy 2c in gpu_detection.py only
# runs when this library is present (otherwise synthesising a vendor JSON
# would be pointing at a non-existent target).  Exposed in the diagnostic
# bundle so users / upstream issue readers can see whether Strategy 2c
# could have helped.
_LIBEGL_NVIDIA_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/aarch64-linux-gnu/libEGL_nvidia.so*",
    "/usr/lib/libEGL_nvidia.so*",
    "/usr/lib64/libEGL_nvidia.so*",
)

# Standard locations the GLVND libEGL dispatcher searches for vendor
# configs.  nvidia-container-toolkit injects its NVIDIA config at
# /usr/share/glvnd/egl_vendor.d/10_nvidia.json when the ``graphics``
# capability is set; some setups instead drop it under /etc/glvnd.  Both
# are checked so the diagnostic bundle can show whether the file is
# present before Strategy 2c synthesises a replacement.
_NVIDIA_EGL_VENDOR_JSON_SEARCH_PATHS = (
    "/usr/share/glvnd/egl_vendor.d/10_nvidia.json",
    "/etc/glvnd/egl_vendor.d/10_nvidia.json",
)

_VENDOR_LABELS = {"INTEL": "Intel", "AMD": "AMD"}
_LOADER_FAILURE_RE = re.compile(r"failed with error code -?\d+")

_NVIDIA_CHECKS = {
    "nvidia_no_graphics_cap": (
        "The container has no NVIDIA Vulkan driver",
        "The container was started without the 'graphics' NVIDIA driver capability, so the NVIDIA Container Toolkit "
        "didn't add NVIDIA's Vulkan driver.",
        "Set NVIDIA_DRIVER_CAPABILITIES=all and restart the container.",
    ),
    "nvidia_graphics_libs_missing": (
        "NVIDIA's graphics driver files aren't in the container",
        "NVIDIA_DRIVER_CAPABILITIES asks for graphics and CUDA works, but none of NVIDIA's graphics driver files "
        "(libGLX_nvidia.so, libEGL_nvidia.so, libnvidia-glvkspirv.so) reached the container, so there is no NVIDIA "
        "Vulkan driver to use.",
        "Look for them on the host: find / -xdev -name 'libGLX_nvidia.so*' 2>/dev/null. If that finds them, the "
        "host's NVIDIA container setup isn't passing them in: mount them into the container yourself ("
        f"{NVIDIA_GRAPHICS_FILES_DOCS} has the lines to add). If it finds nothing, the host's NVIDIA driver was "
        "installed without its graphics part: install the full driver.",
    ),
    "nvidia_icd_missing": (
        "NVIDIA's Vulkan driver file is missing",
        "NVIDIA_DRIVER_CAPABILITIES asks for graphics, but nvidia_icd.json isn't inside the container.",
        "If the host NVIDIA driver is 570 to 579, upgrade it to 580 or newer (those versions leave the file out); "
        'otherwise switch the NVIDIA runtime to legacy mode (mode = "legacy" in '
        "/etc/nvidia-container-runtime/config.toml). Then restart.",
    ),
    "nvidia_spirv_missing": (
        "NVIDIA's Vulkan driver is missing a library",
        "nvidia_icd.json is present, but libnvidia-glvkspirv.so, which it needs, isn't.",
        'Switch the NVIDIA runtime to legacy mode (mode = "legacy" in /etc/nvidia-container-runtime/config.toml) '
        "and restart the container.",
    ),
    "nvidia_loader_rejected": (
        "The Vulkan loader rejected the NVIDIA driver",
        "The graphics capability, nvidia_icd.json and libnvidia-glvkspirv.so are all in place, but the loader still "
        "rejected the driver.",
        "Copy the diagnostic bundle into a GitHub issue.",
    ),
}


@dataclass(frozen=True)
class VulkanCheck:
    """What the app found out about one GPU's Vulkan.

    Attributes:
        ok: Vulkan runs on this GPU.
        cause: Short machine name (``ready`` when ok).
        title: One line for a headline.
        why: What was found, in a sentence or two.
        fix: What the user can do, in a sentence or two (empty when ok).
        facts: Raw values for the diagnostic bundle only; :meth:`to_dict` leaves them out so they never reach a
            browser or the app.
    """

    ok: bool
    cause: str
    title: str
    why: str
    fix: str
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """The check as a JSON-able dict, without ``facts`` (uid, groups, kernel release stay in the bundle)."""
        return {
            "ok": self.ok,
            "cause": self.cause,
            "title": self.title,
            "why": self.why,
            "fix": self.fix,
        }


def diagnose_nvidia_environment() -> dict[str, Any]:
    """Gather facts about the container's Vulkan configuration.

    Used by the web page to dispatch the pure-NVIDIA warning
    branch into one of five sub-cases (A1 / A1b / A2 / A3 / A4) naming the
    specific misconfiguration rather than a generic "upstream packaging
    issue" — and by the ``GET /api/system/vulkan/debug`` endpoint to
    produce a plain-text diagnostic bundle users can paste into GitHub
    issues.

    All facts are derived from ``os.environ`` and the filesystem, so
    this is safe to call from any thread and does not perform any
    expensive subprocess work.

    Returns:
        dict with the following keys:

        - ``nvidia_capabilities`` (str or None) — the raw value of the
          ``NVIDIA_DRIVER_CAPABILITIES`` env var, or None if unset.
        - ``nvidia_capabilities_has_graphics`` (bool) — True if the
          capability string contains ``graphics`` (or is ``all``).
        - ``nvidia_icd_json_path`` (str or None) — the first path in
          :data:`_NVIDIA_ICD_JSON_SEARCH_PATHS` where
          ``nvidia_icd.json`` exists, or None.
        - ``libnvidia_glvkspirv_found`` (bool) — True if a
          ``libnvidia-glvkspirv.so*`` file is present in any of the
          standard library paths.
        - ``nvidia_drm_loaded`` (bool) — True if ``/proc/driver/nvidia``
          exists, meaning the host's NVIDIA kernel module is loaded and
          exposed to the container.
        - ``nvidia_driver_version`` (str or None) — parsed from
          ``/proc/driver/nvidia/version`` if available. Surfaced in the
          debug bundle and in the Case A2 warning that blames a specific
          driver version range.
    """
    caps = os.environ.get("NVIDIA_DRIVER_CAPABILITIES")
    caps_lower = (caps or "").lower()
    caps_has_graphics = "graphics" in caps_lower or caps_lower == "all"

    nvidia_icd_json_path: str | None = None
    for path in _NVIDIA_ICD_JSON_SEARCH_PATHS:
        if os.path.exists(path):
            nvidia_icd_json_path = path
            break

    libnvidia_glvkspirv_found = False
    for pattern in _LIBNVIDIA_GLVKSPIRV_GLOBS:
        if glob.glob(pattern):
            libnvidia_glvkspirv_found = True
            break

    libegl_nvidia_found = False
    for pattern in _LIBEGL_NVIDIA_GLOBS:
        if glob.glob(pattern):
            libegl_nvidia_found = True
            break

    nvidia_egl_vendor_json_path: str | None = None
    for path in _NVIDIA_EGL_VENDOR_JSON_SEARCH_PATHS:
        if os.path.exists(path):
            nvidia_egl_vendor_json_path = path
            break

    nvidia_drm_loaded = os.path.exists("/proc/driver/nvidia")
    nvidia_driver_version: str | None = None
    version_path = "/proc/driver/nvidia/version"
    if os.path.exists(version_path):
        try:
            with open(version_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
            # Typical line: "NVRM version: NVIDIA UNIX x86_64 Kernel Module  570.133.07  Thu Mar 20 14:50:40 UTC 2025"
            for line in content.splitlines():
                if "NVRM version" in line:
                    parts = line.split()
                    for part in parts:
                        # Match e.g. "570.133.07" or "550.54.15"
                        if "." in part and part.replace(".", "").isdigit():
                            nvidia_driver_version = part
                            break
                    if nvidia_driver_version:
                        break
        except OSError as exc:
            logger.debug("Could not read {}: {}", version_path, exc)

    return {
        "nvidia_capabilities": caps,
        "nvidia_capabilities_has_graphics": caps_has_graphics,
        "nvidia_icd_json_path": nvidia_icd_json_path,
        "libnvidia_glvkspirv_found": libnvidia_glvkspirv_found,
        "libegl_nvidia_found": libegl_nvidia_found,
        "nvidia_egl_vendor_json_path": nvidia_egl_vendor_json_path,
        "nvidia_drm_loaded": nvidia_drm_loaded,
        "nvidia_driver_version": nvidia_driver_version,
    }


def nvidia_cause(diag: dict[str, Any]) -> str:
    """Which of the five NVIDIA container problems applies.

    Args:
        diag: The container facts (``nvidia_capabilities_has_graphics``, ``nvidia_icd_json_path``,
            ``libnvidia_glvkspirv_found``, ``libegl_nvidia_found``).

    Returns:
        ``nvidia_no_graphics_cap``, ``nvidia_graphics_libs_missing``, ``nvidia_icd_missing``,
        ``nvidia_spirv_missing`` or ``nvidia_loader_rejected`` (everything is in place and the loader still said no).
    """
    if not diag["nvidia_capabilities_has_graphics"]:
        return "nvidia_no_graphics_cap"
    # Nothing from the graphics capability arrived at all (issue #419): the host driver has no graphics part, or its
    # container setup doesn't pass it on. Only the JSON missing while the libraries are there is what nvidia_icd_missing
    # names (and with no other GPU, the probe writes that JSON itself first).
    if (
        diag["nvidia_icd_json_path"] is None
        and not diag["libnvidia_glvkspirv_found"]
        and not diag["libegl_nvidia_found"]
    ):
        return "nvidia_graphics_libs_missing"
    if diag["nvidia_icd_json_path"] is None:
        return "nvidia_icd_missing"
    if not diag["libnvidia_glvkspirv_found"]:
        return "nvidia_spirv_missing"
    return "nvidia_loader_rejected"


def loader_failure_line(debug_buffer: str) -> str | None:
    """The Vulkan loader's last ``failed with error code -N`` line in the captured debug output, if any."""
    lines = [line.strip() for line in (debug_buffer or "").splitlines() if _LOADER_FAILURE_RE.search(line)]
    return lines[-1] if lines else None


def drm_driver_of(node: str) -> str | None:
    """The kernel driver bound to a render node (``i915``, ``amdgpu``, ``radeon``, ...), None when sysfs can't say."""
    link = os.path.realpath(f"/sys/class/drm/{os.path.basename(node)}/device/driver")
    name = os.path.basename(link)
    return None if name == "driver" else name


def read_i915_params(fd: int) -> dict[str, int | None]:
    """Ask the i915 kernel driver for every parameter Mesa's Vulkan driver requires.

    Only for a node bound to i915: other drivers answer every GETPARAM with EINVAL.

    Args:
        fd: An open render node.

    Returns:
        Parameter name to its value, None where the ioctl failed. Includes ``I915_PARAM_HAS_LLC``.
    """
    import fcntl  # POSIX only; an i915 render node never exists on Windows, where the node never gets here

    wanted = [("I915_PARAM_HAS_LLC", I915_PARAM_HAS_LLC)] + [
        (name, number) for name, number, _, _ in I915_REQUIRED_PARAMS
    ]
    params: dict[str, int | None] = {}
    for name, number in wanted:
        value = ctypes.c_int(0)
        request = _GetParam(number, ctypes.pointer(value))
        try:
            fcntl.ioctl(fd, DRM_IOCTL_I915_GETPARAM, request)
        except OSError:
            params[name] = None
        else:
            params[name] = value.value
    return params


class _GetParam(ctypes.Structure):
    """``struct drm_i915_getparam``: the parameter number, then where the kernel writes its value."""

    _fields_ = (("param", ctypes.c_int32), ("value", ctypes.POINTER(ctypes.c_int)))


def missing_i915_features(params: dict[str, int | None]) -> list[tuple[str, str]]:
    """The required i915 features the kernel lacks, as ``(label, since)`` in the order Mesa's list gives them.

    Args:
        params: The result of :func:`read_i915_params`.
    """
    missing = []
    for name, _number, label, since in I915_REQUIRED_PARAMS:
        value = params.get(name)
        if name == "I915_PARAM_MMAP_VERSION":
            # Only needed when the GPU has no shared last-level cache; without LLC the CPU maps through the GTT.
            if params.get("I915_PARAM_HAS_LLC"):
                continue
            absent = not value or value < 1
        else:
            absent = not value
        if absent:
            missing.append((label, since))
    return missing


def _process_identity() -> dict[str, Any]:
    return {"uid": os.getuid(), "gid": os.getgid(), "groups": sorted(os.getgroups())}


def _check_render_node(node: str, vendor: str, loader_line: str | None) -> VulkanCheck:
    """Work out why a GPU behind ``node`` (Intel or AMD) has no hardware Vulkan."""
    label = _VENDOR_LABELS.get(vendor, vendor)
    facts: dict[str, Any] = {"node": node, **_process_identity(), "release": os.uname().release}
    try:
        node_stat = os.stat(node)
    except OSError:
        facts["opened"] = False
        return VulkanCheck(
            False,
            "no_render_node",
            "/dev/dri isn't passed into the container",
            f"{node} doesn't exist inside the container.",
            "Add --device /dev/dri:/dev/dri, then restart.",
            facts,
        )
    try:
        fd = os.open(node, os.O_RDWR)
    except OSError as exc:
        facts["opened"] = False
        facts["errno"] = errno.errorcode.get(exc.errno or 0, str(exc.errno))
        if exc.errno in (errno.EACCES, errno.EPERM):
            gid = node_stat.st_gid
            groups = ", ".join(str(g) for g in facts["groups"]) or "none"
            facts["node_gid"] = gid
            return VulkanCheck(
                False,
                "cant_open",
                f"The app can't open {node}",
                f"Opening {node} failed: {os.strerror(exc.errno)}.",
                f"It belongs to group {gid}; the app runs as {facts['uid']}:{facts['gid']} in groups {groups}. "
                f"Add --group-add {gid} (compose: group_add: ['{gid}']), then restart.",
                facts,
            )
        return _driver_refused(
            label, facts, loader_line, f"Opening {node} failed: {exc.strerror or exc}.", kernel_checked=False
        )
    try:
        facts["opened"] = True
        driver = drm_driver_of(node)
        facts["driver"] = driver
        if driver == "radeon":
            return VulkanCheck(
                False,
                "old_amd_driver",
                "This AMD GPU uses the old radeon driver",
                f"{node} is driven by the radeon kernel driver.",
                "AMD's Vulkan driver needs the amdgpu kernel driver (host setting).",
                facts,
            )
        if driver == "i915":
            params = read_i915_params(fd)
            missing = missing_i915_features(params)
            facts["params"] = params
            facts["missing"] = [label_ for label_, _ in missing]
            if missing:
                features = ", ".join(label_ for label_, _ in missing)
                since = max((since_ for _, since_ in missing), key=_version_key)
                return VulkanCheck(
                    False,
                    "kernel_too_old",
                    "The host's Linux kernel is too old",
                    f"The host runs Linux {facts['release']}. Intel's Vulkan driver needs {features} "
                    f"(Linux {since} or newer).",
                    "Nothing in the container can add it. A host OS update with a newer kernel fixes it; until then "
                    "credits are read on the CPU.",
                    facts,
                )
    finally:
        os.close(fd)
    return _driver_refused(label, facts, loader_line, kernel_checked=driver == "i915")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _driver_refused(
    label: str, facts: dict[str, Any], loader_line: str | None, why: str = "", *, kernel_checked: bool
) -> VulkanCheck:
    if loader_line:
        facts["loader_line"] = loader_line
        loader_sentence = f"The Vulkan loader reported: {loader_line.rstrip('.')}."
        why = f"{why} {loader_sentence}" if why else loader_sentence
    # Only i915's kernel features are checked; for any other driver the app can't vouch for the kernel.
    fix = "Copy the diagnostic bundle into a GitHub issue."
    if kernel_checked:
        fix = "The host kernel has every feature the app checks for. " + fix
    return VulkanCheck(
        False,
        "driver_refused",
        f"The {label} Vulkan driver refused this GPU",
        why or "Vulkan listed no hardware GPU for it.",
        fix,
        facts,
    )


def _device_vendor(device: str) -> str | None:
    lowered = device.lower().lstrip()
    if lowered.startswith("intel"):
        return "INTEL"
    if lowered.startswith(("amd", "radeon")):
        return "AMD"
    if _is_nvidia_vulkan_device(device):
        return "NVIDIA"
    return None


def check_gpus(
    gpus: list[dict[str, Any]],
    probe: VulkanProbeResult,
    *,
    ffmpeg_has_vulkan: bool = True,
    nvidia_diag: Callable[[], dict[str, Any]] | None = None,
    debug_buffer: str = "",
) -> list[VulkanCheck | None]:
    """Decide, per GPU, whether it can use Vulkan and if not why.

    Args:
        gpus: The detected GPUs (``type``, ``name``, ``device``, ``status``).
        probe: The cached Vulkan probe result.
        ffmpeg_has_vulkan: Whether this FFmpeg build has Vulkan at all.
        nvidia_diag: Reads the container facts :func:`nvidia_cause` needs; called only when an NVIDIA GPU needs it.
        debug_buffer: The loader's debug capture, for its own failing line.

    Returns:
        One entry per GPU, in order. None where nothing is known about that GPU (including every GPU whose vendor is
        not the one the probe found, when the probe found hardware).
    """
    if probe.device is not None and not probe.is_software:
        ready_vendor = _device_vendor(probe.device)
        return [
            VulkanCheck(True, "ready", "Vulkan ready", probe.device, "", {})
            if gpu.get("type") == ready_vendor
            else None
            for gpu in gpus
        ]
    loader_line = loader_failure_line(debug_buffer)
    checks: list[VulkanCheck | None] = []
    for gpu in gpus:
        vendor = gpu.get("type")
        if vendor not in ("NVIDIA", "INTEL", "AMD"):
            checks.append(None)
        elif not ffmpeg_has_vulkan:
            checks.append(
                VulkanCheck(
                    False,
                    "no_vulkan_in_ffmpeg",
                    "This FFmpeg has no Vulkan",
                    "The FFmpeg in this container was built without Vulkan support.",
                    "This is a problem with the container image. Please report it on GitHub.",
                    {},
                )
            )
        elif vendor == "NVIDIA":
            cause = nvidia_cause(nvidia_diag()) if nvidia_diag else "nvidia_loader_rejected"
            title, why, fix = _NVIDIA_CHECKS[cause]
            checks.append(VulkanCheck(False, cause, title, why, fix, {}))
        else:
            checks.append(_check_render_node(gpu.get("device") or "", vendor, loader_line))
    return checks


_cache_lock = threading.Lock()
_cache: dict[tuple, list[VulkanCheck | None]] = {}
# One compute at a time: the probe behind it can take seconds, and two callers shouldn't both run it.
_compute_lock = threading.Lock()
# Bumped by a clear, so a compute that began before it doesn't store an answer the clear meant to drop.
_generation = 0


def clear_vulkan_check_cache() -> None:
    """Forget the per-GPU checks (a GPU re-scan calls this)."""
    global _generation
    with _cache_lock:
        _cache.clear()
        _generation += 1


def get_vulkan_checks(
    gpus: list[dict[str, Any]], nvidia_diag: Callable[[], dict[str, Any]] | None = None
) -> list[VulkanCheck | None]:
    """The cached per-GPU Vulkan checks for these GPUs (see :func:`check_gpus`).

    Cached per (probe device, GPU device list) since the container's devices and kernel don't change while it runs.
    """
    from .ffmpeg_capabilities import _is_hwaccel_available

    probe = get_vulkan_device_info()
    key = (probe.device, tuple((gpu.get("type"), gpu.get("device")) for gpu in gpus))
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    with _compute_lock:
        with _cache_lock:
            cached = _cache.get(key)
            generation = _generation
        if cached is not None:
            return cached
        checks = check_gpus(
            gpus,
            probe,
            ffmpeg_has_vulkan=_is_hwaccel_available("vulkan"),
            nvidia_diag=nvidia_diag,
            debug_buffer=get_vulkan_debug_buffer(),
        )
        with _cache_lock:
            if generation == _generation:
                _cache[key] = checks
    return checks


def startup_warning(gpu: dict[str, Any], check: VulkanCheck) -> str:
    """The one-line startup WARNING for a GPU that can't use Vulkan."""
    from ..markers.credits.textdet_helper import _WEBGPU_VENDORS

    credits_clause = (
        "credits text is read on the CPU (just as accurate, slower) and " if gpu.get("type") in _WEBGPU_VENDORS else ""
    )
    return (
        f"Vulkan: {gpu.get('name')} ({gpu.get('device')}) can't use Vulkan: {check.title}. {check.why} "
        f"Effect: {credits_clause}Dolby Vision Profile 5 thumbnails get a green and purple tint; previews still use "
        f"the GPU. {check.fix}"
    )


def log_vulkan_warnings(gpus: list[dict[str, Any]], nvidia_diag: Callable[[], dict[str, Any]] | None = None) -> None:
    """Log one WARNING per GPU that can't use Vulkan. Never raises: it only informs."""
    try:
        for gpu, check in zip(gpus, get_vulkan_checks(gpus, nvidia_diag), strict=True):
            if check is not None and not check.ok:
                logger.warning(startup_warning(gpu, check))
    except Exception as exc:  # noqa: BLE001 - a log line must never stop startup
        logger.debug("Couldn't check why Vulkan is unavailable: {}", exc)
