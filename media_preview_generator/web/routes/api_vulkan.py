"""Vulkan diagnostics API routes.

Hosts the ``/system/vulkan`` and ``/system/vulkan/debug`` endpoints used by
the dashboard's "Dolby Vision Profile 5 wrong colours" warning banner and
the GitHub-issue-bundle copy button. Split out of ``api_system.py`` because
~640 lines of NVIDIA-ICD-path probing and loader-debug capture are
unrelated to the rest of the system endpoints (status / config / health /
log history) and were drowning them in a single 1.8K-LOC file.

Talks to :mod:`media_preview_generator.gpu.vulkan_probe` for the actual
Vulkan device probe; everything in this module is presentation +
diagnosis (mapping the probe result into a user-actionable warning).
"""

import glob
import html as html_escape
import os

from flask import jsonify
from loguru import logger

from ...gpu.vulkan_check import diagnose_nvidia_environment, get_vulkan_checks, nvidia_cause
from ..auth import setup_or_auth_required
from . import api
from ._helpers import _ensure_gpu_cache


def _vendor_display_name(gpus: list, vendor: str) -> str:
    """Return a display string for the first GPU matching ``vendor``.

    Falls back to a generic ``"<Vendor> GPU"`` label if no readable name
    is present in the cache, so the warning text stays grammatical even
    when the detection layer only returned the vendor code.
    """
    for g in gpus:
        if g.get("type") != vendor:
            continue
        name = (g.get("name") or "").strip()
        if name and name != vendor:
            return name
        break
    return {
        "NVIDIA": "NVIDIA GPU",
        "INTEL": "Intel GPU",
        "AMD": "AMD GPU",
    }.get(vendor, f"{vendor} GPU")


def _diagnose_vulkan_environment() -> dict:
    """The container's NVIDIA Vulkan facts, for the A1-A4 cases and the diagnostic bundle (see
    :func:`...gpu.vulkan_check.diagnose_nvidia_environment`)."""
    return diagnose_nvidia_environment()


def gpus_with_vulkan(gpus: list[dict]) -> list[dict]:
    """Copies of the detected GPUs with a ``vulkan`` key: the GPU's Vulkan check as a dict, or None when unknown.

    Computed per response and never stored in the GPU cache, so a re-scan or a changed probe shows at once.
    """
    try:
        checks = get_vulkan_checks(gpus, _diagnose_vulkan_environment)
    except Exception as exc:  # noqa: BLE001 - the check only adds detail to the GPU list
        logger.warning(
            "Couldn't work out why Vulkan is unavailable ({}: {}). The GPU list is shown without that detail; "
            "GPU detection itself isn't affected.",
            type(exc).__name__,
            exc,
        )
        checks = [None] * len(gpus)
    return [{**gpu, "vulkan": check.to_dict() if check else None} for gpu, check in zip(gpus, checks, strict=True)]


def _in_icd_regression_range(driver_version: str) -> bool:
    """Whether an NVIDIA driver version is one of the 570.x-579.x releases that leave nvidia_icd.json out."""
    major = driver_version.split(".", 1)[0]
    return major.isdigit() and 570 <= int(major) <= 579


def _nvidia_body_html(cause: str, diag: dict, nvidia_name: str) -> str:
    """The HTML explaining one NVIDIA Vulkan failure (cases A1, A1b, A2-A4) and how to fix it.

    Args:
        cause: One of the ``nvidia_*`` causes from :func:`...gpu.vulkan_check.nvidia_cause`.
        diag: The facts from :func:`_diagnose_vulkan_environment`.
        nvidia_name: The GPU's display name (escaped here).

    Returns:
        HTML starting with the GPU's name, with no page header or footer.
    """
    nvidia_name_esc = html_escape.escape(nvidia_name)
    if cause == "nvidia_no_graphics_cap":
        # Case A1: NVIDIA_DRIVER_CAPABILITIES is missing 'graphics'.
        # nvidia-container-toolkit didn't inject the Vulkan ICD at all.
        # This is ~80% of real pure-NVIDIA reports.
        current_caps = diag["nvidia_capabilities"] or "(unset)"
        logger.debug(
            "Vulkan warning: selected Case A1 (missing 'graphics' capability) for {!r}; NVIDIA_DRIVER_CAPABILITIES={!r}",
            nvidia_name,
            current_caps,
        )
        body = (
            f"<strong>Your GPU:</strong> {nvidia_name_esc}"
            "<br><br>"
            "Your NVIDIA card is working for video decoding, but "
            "the container was started <strong>without the "
            "<code>graphics</code> NVIDIA driver capability</strong>. "
            "The NVIDIA Container Toolkit only injects the NVIDIA "
            "Vulkan driver into the container when <code>graphics</code> "
            "(or <code>all</code>) is declared; the "
            "<code>compute,video,utility</code> trio that people "
            "usually start with only covers CUDA, NVDEC/NVENC, and "
            "<code>nvidia-smi</code>."
            "<br><br>"
            f'<div class="small">Your container\'s current value: '
            f"<code>NVIDIA_DRIVER_CAPABILITIES={html_escape.escape(current_caps)}</code>"
            "</div>"
            "<br>"
            '<span class="small"><strong>Fix</strong> — change '
            "<code>NVIDIA_DRIVER_CAPABILITIES</code> to "
            "<code>all</code> and restart the container:</span>"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Docker run:</strong> add "
            "<code>-e NVIDIA_DRIVER_CAPABILITIES=all</code></li>"
            "<li><strong>Docker Compose:</strong> under "
            "<code>environment:</code>, add "
            "<code>- NVIDIA_DRIVER_CAPABILITIES=all</code></li>"
            "<li><strong>Unraid:</strong> set "
            "<em>NVIDIA Driver Capabilities</em> to <code>all</code> "
            "in the template</li>"
            "</ul>"
            '<div class="small mt-2">This is the single most common '
            "cause of this warning on pure-NVIDIA hosts and will "
            "almost certainly fix it. After the restart those "
            "thumbnails come out with the right colours.</div>"
        )
    elif cause == "nvidia_graphics_libs_missing":
        # Case A1b (issue #419): graphics is asked for, CUDA works, but not one graphics file arrived — not the
        # ICD JSON, not libEGL_nvidia, not glvkspirv. The runtime wasn't asked (--gpus all alone on a CDI Docker)
        # or the host driver has no graphics part to give (Synology / NAS driver packages).
        logger.debug(
            "Vulkan warning: selected Case A1b (graphics cap set but no NVIDIA graphics files at all) for {!r}",
            nvidia_name,
        )
        body = (
            f"<strong>Your GPU:</strong> {nvidia_name_esc}"
            "<br><br>"
            "<code>NVIDIA_DRIVER_CAPABILITIES</code> asks for graphics and CUDA works, but none of NVIDIA's "
            "graphics driver files (<code>nvidia_icd.json</code>, <code>libEGL_nvidia.so</code>, "
            "<code>libnvidia-glvkspirv.so</code>) are inside the container, so there is no NVIDIA Vulkan "
            "driver to use."
            "<br><br>"
            '<span class="small"><strong>Fix:</strong></span>'
            '<ul class="small mb-0 mt-1">'
            "<li>If you start the container with <code>--gpus all</code> alone, also add "
            "<code>--runtime=nvidia</code> (Docker Compose: <code>runtime: nvidia</code>) and restart.</li>"
            "<li>If it already uses the NVIDIA runtime, the host's NVIDIA driver doesn't include its Vulkan "
            "part. Some NAS driver packages, such as Synology's, leave it out. Installing a full NVIDIA driver "
            "on the host fixes it; otherwise this host can't tone map those thumbnails.</li>"
            "</ul>"
        )
    elif cause == "nvidia_icd_missing":
        # Case A2: graphics capability is set and NVIDIA's graphics libraries arrived, but the ICD JSON didn't.
        # Driver 570-579 leave it out (nvidia-container-toolkit#1041); otherwise it's a CDI manifest bug (#1559).
        driver_version = diag["nvidia_driver_version"]
        logger.debug(
            "Vulkan warning: selected Case A2 (graphics cap set but nvidia_icd.json missing) for {!r}; driver_version={!r}",
            nvidia_name,
            driver_version,
        )
        regression = (
            "a known regression on NVIDIA driver versions "
            "<strong>570.x – 579.x</strong> where the ICD is not "
            "injected into containers "
            '(<a href="https://github.com/NVIDIA/nvidia-container-toolkit/issues/1041" '
            'target="_blank" rel="noopener">'
            "nvidia-container-toolkit#1041</a>). "
            "<strong>Fix:</strong> upgrade your host NVIDIA driver "
            "to <strong>580 or newer</strong>, or downgrade to "
            "<strong>550</strong>."
        )
        cdi = (
            "your NVIDIA runtime is using the CDI mode and its CDI "
            "manifest is missing Vulkan files "
            '(<a href="https://github.com/NVIDIA/nvidia-container-toolkit/issues/1559" '
            'target="_blank" rel="noopener">'
            "nvidia-container-toolkit#1559</a>). Switch to legacy "
            'mode by setting <code>mode = "legacy"</code> in '
            "<code>/etc/nvidia-container-runtime/config.toml</code> "
            "on the host."
        )
        if driver_version and not _in_icd_regression_range(driver_version):
            causes = (
                f'<span class="small"><strong>Likely cause:</strong> {cdi}</span>'
                f'<div class="small mt-2">Detected host NVIDIA driver: '
                f"<code>{html_escape.escape(driver_version)}</code> (not one of the 570.x–579.x versions "
                "that leave the file out).</div>"
            )
        else:
            driver_line = ""
            if driver_version:
                driver_line = (
                    f'<div class="small mt-2">Detected host NVIDIA driver: '
                    f"<code>{html_escape.escape(driver_version)}</code>, one of the versions with this "
                    "regression.</div>"
                )
            causes = (
                f'<span class="small"><strong>Most likely cause:</strong> {regression}</span>'
                f'<br><br><span class="small"><strong>Less likely cause:</strong> if {cdi}</span>'
                f"{driver_line}"
            )
        body = (
            f"<strong>Your GPU:</strong> {nvidia_name_esc}"
            "<br><br>"
            "<code>NVIDIA_DRIVER_CAPABILITIES</code> looks correct, "
            "but the NVIDIA Vulkan ICD file "
            "(<code>nvidia_icd.json</code>) is not present inside "
            "the container. The NVIDIA Container Toolkit should "
            "have injected it and didn't."
            "<br><br>"
            f"{causes}"
        )
    elif cause == "nvidia_spirv_missing":
        # Case A3: ICD JSON exists but the supporting library is
        # missing. Almost always the CDI manifest bug (#1559).
        logger.debug(
            "Vulkan warning: selected Case A3 (nvidia_icd.json at {} but libnvidia-glvkspirv not found) for {!r}",
            diag["nvidia_icd_json_path"],
            nvidia_name,
        )
        body = (
            f"<strong>Your GPU:</strong> {nvidia_name_esc}"
            "<br><br>"
            "Your NVIDIA Vulkan driver file is present (found at "
            f"<code>{html_escape.escape(diag['nvidia_icd_json_path'])}</code>) "
            "but a required supporting library — "
            "<code>libnvidia-glvkspirv.so</code> — is not. The ICD "
            "loads and then immediately fails to resolve its own "
            "dependencies, so the loader discards it and falls back "
            "to software rendering."
            "<br><br>"
            '<span class="small"><strong>Root cause:</strong> a '
            "known NVIDIA Container Toolkit bug where the CDI "
            "manifest only lists the ICD JSON, not the library it "
            "depends on "
            '(<a href="https://github.com/NVIDIA/nvidia-container-toolkit/issues/1559" '
            'target="_blank" rel="noopener">'
            "nvidia-container-toolkit#1559</a>).</span>"
            "<br><br>"
            '<span class="small"><strong>Fix:</strong></span>'
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Quick fix:</strong> switch your NVIDIA "
            "runtime to legacy mode by editing "
            "<code>/etc/nvidia-container-runtime/config.toml</code> "
            'on the host and setting <code>mode = "legacy"</code>, '
            "then restart the container.</li>"
            "<li><strong>Or:</strong> regenerate the CDI manifest "
            "with <code>sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml</code> "
            "after upgrading to a toolkit version that includes the "
            "fix.</li>"
            "</ul>"
        )
    else:
        # Case A4: everything on the checklist is correct but the
        # loader still rejected the ICD. This is the "please file
        # an issue with diagnostics" path.
        body = (
            f"<strong>Your GPU:</strong> {nvidia_name_esc}"
            "<br><br>"
            "All the usual NVIDIA container requirements are "
            "satisfied &mdash; the <code>graphics</code> capability "
            "is set, the NVIDIA Vulkan ICD file is present at "
            f"<code>{html_escape.escape(diag['nvidia_icd_json_path'] or '(unknown)')}</code>, "
            "and <code>libnvidia-glvkspirv.so</code> is reachable. "
            "Despite all that, the Vulkan loader still rejected the "
            "NVIDIA driver. This is uncommon."
            "<br><br>"
            '<span class="small"><strong>Please file a GitHub '
            "issue</strong> and include the diagnostic bundle from "
            "<code>GET /api/system/vulkan/debug</code> (there's a "
            "<em>Copy diagnostic bundle</em> button just below this "
            "message). The bundle contains the full "
            "<code>VK_LOADER_DEBUG=all</code> trace, which will "
            "usually show exactly why each ICD was rejected.</span>"
        )
    return body


# The loader-rejected diagnosis is polled every minute; log it once per state change.
_loader_rejected_logged = False


def _get_vulkan_info() -> dict:
    """Return Vulkan device info and warn when DV5 thumbnails will have the wrong colours.

    When the cached Vulkan device from ``get_vulkan_device_info()`` is a
    software rasteriser (``llvmpipe`` / ``lavapipe``), builds a
    GPU-aware HTML warning that leads with the user-visible symptom
    (green-and-purple-tinted Dolby Vision Profile 5 thumbnails), then branches on
    what the user can actually do about it:

    - **Pure NVIDIA** (regardless of ``/dev/dri``): upstream version
      skew between linuxserver/ffmpeg's Vulkan loader and the NVIDIA
      driver. Mounting ``/dev/dri`` on a pure-NVIDIA host *does not
      help*, because there is no Mesa ICD to fall back to — so this
      branch fires whether or not the render node is mapped.
    - **NVIDIA + Intel/AMD, no render node mapped:** mount
      ``/dev/dri`` so the container can reach the Mesa driver.
    - **Intel/AMD only, no render node mapped:** mount ``/dev/dri``.
    - **Intel/AMD (with or without NVIDIA), render node mapped but
      still llvmpipe:** host drivers or render-node permissions issue.
    - **No GPU detected at all:** no hardware visible to the container.

    The shared header avoids jargon like "Vulkan driver misconfigured"
    or "software rasterizer" and explains the mechanism in plain
    English. Per-case bodies name the user's actual GPU. Technical
    details (``VK_ERROR_INCOMPATIBLE_DRIVER``, loader versions, etc.)
    live in a muted footer so curious users can google them without
    cluttering the main message.
    """
    from ...gpu.vulkan_probe import get_vulkan_device_info

    info = get_vulkan_device_info()
    device = info.device
    is_software = info.is_software

    global _loader_rejected_logged
    result: dict = {"device": device}
    if not is_software:
        _loader_rejected_logged = False
        logger.debug("Vulkan warning: device={!r} is_software=False; no DV5 warning will be shown.", device)
        return result

    try:
        gpus = list(_ensure_gpu_cache())
    except Exception as exc:
        logger.warning(
            "Dolby Vision warning: could not list detected GPUs while building the diagnostic message "
            "({}: {}). The warning will still be shown but won't include your GPU model name. "
            "This is cosmetic only — your GPU detection itself isn't affected.",
            type(exc).__name__,
            exc,
        )
        gpus = []

    vendors = {g["type"] for g in gpus if g.get("type")}
    has_nvidia = "NVIDIA" in vendors
    has_intel = "INTEL" in vendors
    has_amd = "AMD" in vendors
    has_mesa_vendor = has_intel or has_amd
    dri_render_nodes = glob.glob("/dev/dri/renderD*")
    dri_mapped = bool(dri_render_nodes)

    logger.debug(
        "Vulkan warning inputs: device={!r} vendors={} has_nvidia={} has_mesa={} dri_render_nodes={}",
        device,
        sorted(vendors),
        has_nvidia,
        has_mesa_vendor,
        dri_render_nodes or "[]",
    )

    loader_rejected = False
    nvidia_name = _vendor_display_name(gpus, "NVIDIA") if has_nvidia else ""
    # Prefer AMD for the Mesa label when both AMD and Intel are present
    # (AMD is more likely to be the user's primary display GPU); either
    # works for the /dev/dri remediation text.
    mesa_vendor_code = "AMD" if has_amd else ("INTEL" if has_intel else "")
    mesa_name = _vendor_display_name(gpus, mesa_vendor_code) if mesa_vendor_code else ""
    mesa_vendor_label = {"INTEL": "Intel", "AMD": "AMD"}.get(mesa_vendor_code, "Mesa")
    all_names_escaped = ", ".join(
        html_escape.escape(g.get("name") or g.get("type") or "GPU") for g in gpus if g.get("name") or g.get("type")
    )

    header = (
        "When this app creates thumbnails for <strong>Dolby Vision "
        "Profile 5</strong> content, it uses the GPU to tone map them "
        "(convert the HDR picture to normal colours and brightness). Your container "
        "does not have a working hardware Vulkan driver for this step, so "
        "those thumbnails are made without tone mapping and come out "
        "with the <strong>wrong colours</strong>, a green and purple tint."
        "<br><br>"
        "All other content (standard video, HDR10, Dolby Vision Profile "
        "7 and 8) is not affected."
        "<br><br>"
    )
    footer = (
        '<div class="small text-muted mt-2">You can safely dismiss this '
        "warning if you have no Dolby Vision Profile 5 content, or if tinted "
        "thumbnails on those titles don't bother you.</div>"
    )

    # Pure-NVIDIA takes precedence over dri_mapped: mounting /dev/dri on
    # a host with no Mesa-capable GPU does nothing on its own, so we
    # never send these users down the /dev/dri path. Instead, dispatch
    # into one of five sub-cases (A1/A1b/A2/A3/A4) based on what
    # _diagnose_vulkan_environment() tells us is actually broken, so the
    # warning names the specific fix and the user can act on it.
    if has_nvidia and not has_mesa_vendor:
        diag = _diagnose_vulkan_environment()
        cause = nvidia_cause(diag)
        logger.debug("Vulkan warning: selected NVIDIA case {} for {!r}", cause, nvidia_name)
        loader_rejected = cause == "nvidia_loader_rejected"
        if loader_rejected and not _loader_rejected_logged:
            logger.warning(
                "Dolby Vision Profile 5 warning: your NVIDIA driver looks correctly installed in this "
                "container ({!r}, all toolkit checks pass), but the Vulkan loader still rejected it. "
                "This is rare — please open a GitHub issue and include the diagnostic bundle from "
                "Settings → Processing Options → GPU device tuning ('Copy diagnostic bundle', or GET "
                "/api/system/vulkan/debug). "
                "Until then, Dolby Vision Profile 5 thumbnails have the wrong colours (no tone mapping); "
                "all other thumbnails are unaffected.",
                nvidia_name,
            )
        body = _nvidia_body_html(cause, diag, nvidia_name)
    elif has_nvidia and has_mesa_vendor and not dri_mapped:
        # NVIDIA + Intel/AMD but /dev/dri not forwarded: mounting the
        # render node lets libplacebo use Mesa alongside NVIDIA decoding.
        logger.debug(
            "Vulkan warning: selected Case B (NVIDIA + Mesa, /dev/dri not mapped) for NVIDIA={!r} Mesa={!r}",
            nvidia_name,
            mesa_name,
        )
        body = (
            f"<strong>Your GPUs:</strong> "
            f"{html_escape.escape(nvidia_name)} and "
            f"{html_escape.escape(mesa_name)}"
            "<br><br>"
            f"Your {mesa_vendor_label} GPU can handle the GPU rendering "
            "step, but the container can't reach it because the "
            "<code>/dev/dri</code> render node isn't forwarded. NVIDIA's "
            "own rendering driver can't be used due to a separate "
            "version-mismatch issue, so those thumbnails skip tone "
            "mapping."
            "<br><br>"
            '<span class="small"><strong>Fix</strong> — add this to '
            "your Docker configuration and restart the container:</span>"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Docker run:</strong> add "
            "<code>--device /dev/dri:/dev/dri</code></li>"
            "<li><strong>Docker Compose:</strong> add "
            "<code>devices: [&quot;/dev/dri:/dev/dri&quot;]</code> "
            "under the service</li>"
            "</ul>"
            '<div class="small mt-2">After the restart, those thumbnails '
            "come out with the right colours. Your NVIDIA card keeps handling "
            "video decoding &mdash; the two paths are independent.</div>"
        )
    elif has_mesa_vendor and not has_nvidia and not dri_mapped:
        # Intel/AMD only, no render node: straight mount fix.
        logger.debug("Vulkan warning: selected Case C (Mesa only, /dev/dri not mapped) for {!r}", mesa_name)
        body = (
            f"<strong>Your GPU:</strong> {html_escape.escape(mesa_name)}"
            "<br><br>"
            "Your GPU can handle the rendering step, but the container "
            "can't reach it because the <code>/dev/dri</code> render "
            "node isn't forwarded."
            "<br><br>"
            '<span class="small"><strong>Fix</strong> — add this to '
            "your Docker configuration and restart the container:</span>"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Docker run:</strong> add "
            "<code>--device /dev/dri:/dev/dri</code></li>"
            "<li><strong>Docker Compose:</strong> add "
            "<code>devices: [&quot;/dev/dri:/dev/dri&quot;]</code> "
            "under the service</li>"
            "</ul>"
            '<div class="small mt-2">After the restart, those thumbnails '
            "come out with the right colours.</div>"
        )
    elif has_mesa_vendor and dri_mapped:
        # Intel/AMD (with or without NVIDIA) already has /dev/dri but
        # rendering still fell back to software. Usually host-side.
        logger.debug(
            "Vulkan warning: selected Case D (Mesa with /dev/dri mapped but rendering still fell back) for {!r}; dri_nodes={}",
            mesa_name,
            dri_render_nodes,
        )
        detected = all_names_escaped or "a GPU"
        body = (
            f"<strong>Your GPUs:</strong> {detected}"
            "<br><br>"
            "The <code>/dev/dri</code> render node is already forwarded "
            "to the container, but the GPU rendering check still "
            "failed. Usually this means one of two things:"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Your host's GPU drivers are missing or "
            "broken.</strong> Run <code>vainfo</code> on the host "
            "(outside the container) &mdash; if it does not list your "
            "GPU, install or fix the host's Mesa drivers.</li>"
            "<li><strong>Render node permissions don't match the "
            "container user.</strong> Run "
            "<code>ls -la /dev/dri/renderD*</code> on the host. The "
            "container runs as <code>PUID:PGID</code>, and the render "
            "node's group (usually <code>render</code> or "
            "<code>video</code>) needs to be readable by that user.</li>"
            "</ul>"
        )
    else:
        # No GPU detected at all.
        logger.debug("Vulkan warning: selected Case E (no GPU detected)")
        body = (
            "<strong>No GPU detected in this container.</strong>"
            "<br><br>"
            "The container has no GPU visible to it, so GPU rendering "
            "isn't possible at all. Make sure your host has a GPU with "
            "drivers installed, and that the GPU is forwarded to the "
            "container:"
            '<ul class="small mb-0 mt-1">'
            "<li><strong>Intel or AMD:</strong> "
            "<code>--device /dev/dri:/dev/dri</code></li>"
            "<li><strong>NVIDIA:</strong> "
            "<code>--runtime=nvidia --gpus all</code></li>"
            "</ul>"
        )

    _loader_rejected_logged = loader_rejected
    result["warning"] = header + body + footer
    return result


def _vulkan_check_lines(gpus: list[dict]) -> list[str]:
    """The diagnostic bundle's lines for each GPU's Vulkan check, with the raw facts behind it."""
    try:
        checks = get_vulkan_checks(gpus, _diagnose_vulkan_environment)
    except Exception as exc:  # noqa: BLE001 - the rest of the bundle is still worth copying
        return [f"  (the check failed: {type(exc).__name__}: {exc})"]
    lines = []
    for gpu, check in zip(gpus, checks, strict=True):
        lines.append(f"  {gpu.get('name', '?')} ({gpu.get('device', '?')})")
        if check is None:
            lines.append("    (not checked: Vulkan found another vendor's GPU, or this GPU has no Vulkan driver here)")
            continue
        lines += [
            f"    ok: {check.ok}",
            f"    cause: {check.cause}",
            f"    title: {check.title}",
            f"    why: {check.why}",
        ]
        facts = dict(check.facts)
        params = facts.pop("params", None) or {}
        lines += [f"    {key}: {value}" for key, value in facts.items()]
        lines += [f"    {name}: {value}" for name, value in params.items()]
    return lines or ["  (no GPUs)"]


def _credits_text_lines() -> list[str]:
    """One line per GPU device the credits text detection has used: its backend and why it is on the CPU."""
    from ...markers.credits.textdet_helper import credits_text_devices

    rows = credits_text_devices()
    return [f"  {row['key']}: {row['backend']}" + (f" ({row['reason']})" if row["reason"] else "") for row in rows] or [
        "  (no credits text detection has run yet)"
    ]


@api.route("/system/vulkan")
def get_vulkan():
    """Return container Vulkan device info and warn if misconfigured for DV5.

    No authentication required — Vulkan device info is not sensitive.
    """
    return jsonify(_get_vulkan_info())


@api.route("/system/vulkan/debug")
@setup_or_auth_required
def get_vulkan_debug():
    """Return a plain-text Vulkan diagnostic bundle for GitHub issue reports.

    Combines the environment diagnosis, the probe result, and the
    captured ``VK_LOADER_DEBUG=all`` stderr (when the probe exhausted
    all strategies) into a single pasteable block. The dashboard and
    settings banners expose a "Copy diagnostic bundle" button that
    fetches this endpoint and writes the response to
    ``navigator.clipboard``.

    Needs a login once setup is done: the bundle names the kernel release, the app's uid/gid and groups, and the
    GPU's kernel parameters.
    """
    from ...gpu.vulkan_probe import (
        get_vulkan_debug_buffer,
        get_vulkan_device_info,
        get_vulkan_env_overrides,
    )

    device_info = get_vulkan_device_info()
    diag = _diagnose_vulkan_environment()
    env_overrides = get_vulkan_env_overrides()
    debug_buffer = get_vulkan_debug_buffer()

    gpus = list(_ensure_gpu_cache())

    gpu_lines = [
        f"  - type={g.get('type', '?')} name={g.get('name', '?')} device={g.get('device', '?')}" for g in gpus
    ] or ["  (none detected)"]

    bundle_lines = [
        "=== media_preview_generator Vulkan diagnostic bundle ===",
        "",
        "Use this block when reporting a GPU that can't use Vulkan (credits text read on the CPU, or Dolby Vision",
        "Profile 5 thumbnails with the wrong colours).",
        "It captures the app's view of your container's Vulkan state,",
        "plus the full VK_LOADER_DEBUG=all trace (if one was captured).",
        "",
        "--- Probe result ---",
        f"device:      {device_info.device}",
        f"is_software: {device_info.is_software}",
        "",
        "--- Detected GPUs (from gpu_detection cache) ---",
        *gpu_lines,
        "",
        "--- Host ---",
        f"kernel release: {os.uname().release}",
        f"uid: {os.getuid()}  gid: {os.getgid()}  groups: {sorted(os.getgroups())}",
        "",
        "--- Vulkan check per GPU ---",
        *_vulkan_check_lines(gpus),
        "",
        "--- Credits text detection ---",
        *_credits_text_lines(),
        "",
        "--- Environment diagnosis ---",
        f"NVIDIA_DRIVER_CAPABILITIES: {diag['nvidia_capabilities']!r}",
        f"  has 'graphics':            {diag['nvidia_capabilities_has_graphics']}",
        f"nvidia_icd_json_path:        {diag['nvidia_icd_json_path']!r}",
        f"libnvidia_glvkspirv_found:   {diag['libnvidia_glvkspirv_found']}",
        f"libegl_nvidia_found:         {diag['libegl_nvidia_found']}",
        f"nvidia_egl_vendor_json_path: {diag['nvidia_egl_vendor_json_path']!r}",
        f"nvidia_drm_loaded:           {diag['nvidia_drm_loaded']}",
        f"nvidia_driver_version:       {diag['nvidia_driver_version']!r}",
        "",
        "--- Render nodes ---",
        f"/dev/dri/renderD*: {glob.glob('/dev/dri/renderD*') or '[]'}",
        "",
        "--- Active Vulkan env overrides ---",
    ]
    if env_overrides:
        bundle_lines.append(
            "  (populated by the Strategy-2 VK_DRIVER_FILES retry; these"
            " env vars are injected into the FFmpeg libplacebo subprocess)"
        )
        for k, v in env_overrides.items():
            bundle_lines.append(f"  {k}={v}")
    else:
        bundle_lines.append("  (none — the default probe found a working Vulkan device, or the retry did not succeed)")
    bundle_lines.append("")

    bundle_lines.append("--- VK_LOADER_DEBUG=all capture ---")
    if debug_buffer:
        bundle_lines.append(f"(last {len(debug_buffer)} bytes of ffmpeg stderr from the Strategy-3 diagnostic probe)")
        bundle_lines.append("")
        bundle_lines.append(debug_buffer)
    else:
        bundle_lines.append(
            "(empty — no diagnostic probe was run; Vulkan either worked"
            " on the default or retry strategy, or Vulkan is unavailable"
            " in this FFmpeg build)"
        )

    bundle_lines.append("")
    bundle_lines.append("=== end bundle ===")

    response_body = "\n".join(bundle_lines)
    return response_body, 200, {"Content-Type": "text/plain; charset=utf-8"}
