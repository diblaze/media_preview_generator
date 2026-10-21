# Vulkan check: say why a GPU can't use Vulkan (issue #408)

Status: approved design (owner picked "Bell card + GPU row", 2026-10-16). Built on top of PR #409
(`stevezau/extra_node`) in worktree `.claude/worktrees/issue-408`, branch `issue-408`; lands on `dev` after #409 merges.

## The problem

A Synology DS920+ (Intel UHD 600, Gemini Lake, DSM 7.3) gets `VK_ERROR_INITIALIZATION_FAILED` from every Intel Vulkan
driver in the image. Mesa 25.2/26.0 anv returns that code during `vkEnumeratePhysicalDevices` on i915 only when the
host kernel's i915 lacks one of five features (`src/intel/vulkan/i915/anv_device.c`,
`anv_i915_physical_device_get_parameters`). Release Mesa never prints which one. The app then:

- says "even after trying NVIDIA-specific fixes" and "on Intel/AMD, pass /dev/dri" (already done);
- points to "Settings → System … diagnostic bundle" (that panel no longer exists);
- shows a bell card titled only about Dolby Vision 5, whose Intel advice is "run vainfo / check permissions";
- shows "GPU keeps failing … check the GPU driver" for every 5 credits files (fixed already, see below).

## What the app checks (new module `media_preview_generator/gpu/vulkan_check.py`)

Pure functions, no web imports. Inputs: the detected GPU dicts (`type`, `name`, `device`, `status`) and the cached
`VulkanProbeResult`. Output per GPU: `VulkanCheck(ok, cause, title, why, fix, facts)` (frozen dataclass, `to_dict()`).
All text is plain (no HTML); renderers escape.

When the probe found a hardware device: only the GPU whose vendor matches the probe device gets `ok=True`,
`title="Vulkan ready"`, `why=<probe device string>`. Other GPUs get no check (None) — we don't know.

When the probe found software (llvmpipe/lavapipe) or nothing, every GPU gets a cause:

| cause | when | title | fix |
|---|---|---|---|
| `no_vulkan_in_ffmpeg` | FFmpeg built without Vulkan | "This FFmpeg has no Vulkan" | report it (container issue) |
| `nvidia_*` | NVIDIA GPU | reuse the A1–A4 diagnosis in `web/routes/api_vulkan.py` (move the decision, not the HTML, into the module: `nvidia_no_graphics_cap`, `nvidia_icd_missing`, `nvidia_spirv_missing`, `nvidia_loader_rejected`) | the existing per-case fix sentence |
| `no_render_node` | Intel/AMD and the GPU's `/dev/dri/renderD*` doesn't exist | "/dev/dri isn't passed into the container" | "Add --device /dev/dri:/dev/dri, then restart." |
| `cant_open` | `os.open(node, O_RDWR)` → EACCES/EPERM | "The app can't open {node}" | "It belongs to group {gid}; the app runs as {uid}:{gid} in groups {groups}. Add --group-add {gid} (compose: group_add: ['{gid}']), then restart." |
| `old_amd_driver` | sysfs driver of the node is `radeon` | "This AMD GPU uses the old radeon driver" | "AMD's Vulkan driver needs the amdgpu kernel driver (host setting)." |
| `kernel_too_old` | sysfs driver `i915` and any required i915 param missing (table below) | "The host's Linux kernel is too old" | "Nothing in the container can add it. A host OS update with a newer kernel fixes it; until then credits are read on the CPU." |
| `driver_refused` | anything else | "The {Intel/AMD} Vulkan driver refused this GPU" | "The host kernel has every feature the app checks for. Copy the diagnostic bundle into a GitHub issue." Include the loader's own failing line if `get_vulkan_debug_buffer()` has one (`failed with error code -N`). |

`why` for `kernel_too_old`: "The host runs Linux {release}. Intel's Vulkan driver needs {feature} (Linux {since}+)." —
name the first missing feature in the table order; list all in `facts["missing"]`.

i915 features anv requires (ioctl `DRM_IOCTL_I915_GETPARAM` = `0xC0106446`, struct `{int32 param; pad; int* value}`;
an ioctl error counts as missing, exactly as Mesa does). Labels match Mesa's "kernel missing …" messages so they google:

| param | number | label | since |
|---|---|---|---|
| `I915_PARAM_HAS_WAIT_TIMEOUT` | 19 | gem wait | 3.6 |
| `I915_PARAM_HAS_EXECBUF2` | 9 | execbuf2 | 2.6.33 |
| `I915_PARAM_MMAP_VERSION` ≥ 1, only when `I915_PARAM_HAS_LLC` (17) is 0 | 30 | wc mmap | 4.0 |
| `I915_PARAM_HAS_EXEC_SOFTPIN` | 37 | softpin | 4.5 |
| `I915_PARAM_HAS_EXEC_FENCE_ARRAY` | 49 | syncobj support | 4.14 |

Driver name: `basename(realpath('/sys/class/drm/<renderDxxx>/device/driver'))`. Kernel: `os.uname().release`
(inside a container this is the host's). Proven on plex (UHD 770, i915, Linux 7.2): every param reads 1; on the
NVIDIA node every GETPARAM is EINVAL (so: never run the i915 check on a non-i915 node).

Cache the per-GPU result keyed by (probe device, GPU device list); re-scan GPUs clears it.

## Where people see it

1. **Startup log** — after GPU detection in `web/app.py` `_warm_gpu`, log one WARNING per GPU with a cause:
   `Vulkan: {name} ({device}) can't use Vulkan: {title}. {why} Effect: credits text is read on the CPU (just as
   accurate, slower) and Dolby Vision Profile 5 thumbnails get a green and purple tint; previews still use the GPU.
   {fix}` (drop the credits clause for GPUs outside `_WEBGPU_VENDORS`). In `vulkan_probe.py`: the strategy-3
   WARNING says "NVIDIA-specific fixes" only when an NVIDIA ICD/EGL file was found, and both probe warnings point to
   "Settings → Processing Options → GPU device tuning" (not "Settings → System"); keep `is_node_mode()` wording.
2. **Bell card** (`_build_vulkan_software_fallback_notification`, keep id `vulkan_software_fallback`, source
   `vulkan_probe` so the Copy diagnostic bundle button stays): title `"{GPU name} can't use Vulkan: {title, lower-cased
   first letter}"`, or `"Vulkan isn't working on {n} GPUs"`. Body sections exactly as approved:
   **What this means** (bullets: credits text on the CPU, same accuracy, slower — only for WebGPU vendors; DV5 tint;
   previews and intros still use the GPU), **Why** (`why`), **What to do** (`fix`; for `kernel_too_old`: "Nothing, if
   the CPU keeps up. A host OS update with a newer kernel fixes it."). Action button: "Open GPU check" →
   `/settings#section-processing`. NVIDIA cases keep their existing detailed HTML as the Why/What-to-do content.
3. **Settings → Processing Options → GPU device tuning** (`renderGpuTuningPanel` in `static/js/gpu_config_panel.js`):
   each row gets capability lines under the name: `✓ Video decoding (previews)  {acceleration}` (or the existing
   failure line) and, when a check exists, `✓ Vulkan  {device}` or `✕ Vulkan (credits text, Dolby Vision 5)  {title}
   Why?`. "Why?" expands an inline panel with why + fix + a "Copy diagnostic bundle" button
   (`copyVulkanDiagnosticBundle`). Data: `/api/system/status` and `/api/system/rescan-gpus` add `vulkan` (the dict or
   null) to each GPU entry — computed per response, never stored in `_gpu_cache`.
4. **Diagnostic bundle** (`/api/system/vulkan/debug`): add `--- Host ---` (kernel release, uid, gid, groups),
   `--- Vulkan check per GPU ---` (cause, title, why, render node, driver, opened, every i915 param value), and
   `--- Credits text detection ---` (each pool device: backend and reason). Header no longer says DV5 only.
5. **Remote nodes** (PR #409): `nodes/server/info.py` adds `vulkan` per GPU from the node's own probe + check;
   `nodes/registry.py` `_clean_gpus` keeps `vulkan` (only known string keys, each capped); the Nodes page GPU list
   (`static/js/nodes.js` `#nodeGpuSection`) renders the same Vulkan line + Why panel (shared JS helper). The node logs
   the same startup WARNING.

## Already done in this worktree (keep, adjust)

- `textdet_helper.py`: `cpu_verdict_keys()` / `credits_cpu_verdict_keys()`; `notifications.py` streak card skips a
  GPU in that set whose last reason is a credits one. **Replace with a reason prefix**, because a node GPU's verdict
  lives in the node's own pool: add `CREDITS_KEPT_ON_CPU_PREFIX = "Credits read on the CPU until restart"` (starts
  with `CREDITS_ON_CPU_PREFIX`); `_ask` reports with it when the device's verdict is `cpu` (no WebGPU path, software
  Vulkan, failed self-test), and with the plain prefix for a failure that is retried (crash, cool-down). The worker
  doesn't count a fallback whose reason starts with `CREDITS_KEPT_ON_CPU_PREFIX` towards the streak
  (`Worker._blame_gpu_for`, local or node GPU; it once was a card-side check on the streak's last reason only); drop
  `cpu_verdict_keys`/`credits_cpu_verdict_keys` and the pool lookup in the card. Keep the tests' intent.

## Tests (mirror existing files)

- `tests/test_vulkan_check.py`: every cause row (mock `os.open`, `os.stat`, sysfs `realpath`, `fcntl.ioctl`,
  `os.uname`); MMAP_VERSION only checked without LLC; ioctl error = missing; hardware probe → only the matching
  vendor's GPU gets ok; the i915 ioctl isn't run for amdgpu/nvidia nodes.
- Bell card: title/body per cause, single vs several GPUs, action href, escaping.
- `/api/system/status` and rescan include `vulkan`; bundle sections present.
- Node info/registry: `vulkan` survives `_clean_gpus`; junk keys dropped.
- Streak card: kept-prefix suppressed for local and `node:` keys; crash reason still shows.
- e2e is not required; run `pytest --no-cov` on touched test files, then the full default suite once.
