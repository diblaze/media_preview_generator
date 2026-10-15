"""FFmpeg ``-vf`` filter-chain builders: the HDR10 zscale tone-map and the Dolby Vision Profile 5 paths.

The SDR / HDR10 / DV Profile 7+8 chains are still assembled inline in
:func:`generator.generate_images` because they depend on the
nested ``_gpu_scale_segment`` helper's captured state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

# Recognised DV5 filter-chain kinds.  Each maps to a specific hardware-path
# produced by :func:`build_dv5_vf`.  Kept as plain strings (not an Enum) so
# existing callers comparing against ``path_kind`` continue to work.
DV5_PATH_INTEL_OPENCL = "opencl_dv5_intel"
DV5_PATH_VAAPI_VULKAN = "libplacebo_vaapi"
DV5_PATH_LIBPLACEBO = "libplacebo_dv5"


def display_matrix(stream: Mapping[str, Any]) -> tuple[int, ...] | None:
    """The display matrix in one stream of ffprobe's JSON (``-show_entries stream_side_data=displaymatrix``).

    ffprobe prints it as three rows of three integers, each row behind its offset (``00000000:``); MP4, MOV and
    Matroska all give it, rotation and mirroring alike.

    Args:
        stream: One entry of ffprobe's ``streams`` list.

    Returns:
        The nine numbers row by row, or None when the stream has none or it can't be read.
    """
    side_data_list = stream.get("side_data_list")
    for side_data in side_data_list if isinstance(side_data_list, list) else []:
        text = side_data.get("displaymatrix") if isinstance(side_data, Mapping) else None
        if not isinstance(text, str):
            continue
        try:
            numbers = tuple(
                int(value) for line in text.splitlines() if ":" in line for value in line.split(":")[1].split()
            )
        except ValueError:
            return None
        return numbers if len(numbers) == 9 else None
    return None


def rotation_filter(matrix: Sequence[int] | None) -> str | None:
    """The filters FFmpeg's own autorotation puts in front of the filter graph for a stream with this display matrix,
    for frames in memory: it skips frames that stay on the GPU, so those are turned with these after their download.

    The same reading of the matrix as ``fftools`` (``get_rotation`` and ``configure_input_video_filter``): the angle
    picks the turn, and the signs of its cells pick the mirroring that goes with it. A turn that starts with ``vflip``
    gets a ``copy`` in front, the same pixels: ``vflip`` hands the filter before it a bottom-up buffer, which
    ``hwdownload`` can't fill (CUDA fails every frame with ``CUDA_ERROR_INVALID_VALUE``).

    Args:
        matrix: The stream's display matrix (:func:`display_matrix`), or None.

    Returns:
        The filters, or None for a stream shown as stored, and for any angle other than a quarter turn (FFmpeg turns
        those with ``rotate``, which the GPU paths don't mirror).
    """
    if matrix is None or len(matrix) != 9:
        return None
    scale_x, scale_y = math.hypot(matrix[0], matrix[3]), math.hypot(matrix[1], matrix[4])
    if not scale_x or not scale_y:
        return None
    theta = round(math.degrees(math.atan2(matrix[1] / scale_y, matrix[0] / scale_x)))
    theta -= 360 * math.floor(theta / 360 + 0.9 / 360)
    if theta == 90:
        return "transpose=cclock_flip" if matrix[3] > 0 else "transpose=clock"
    if theta == 270:
        return "transpose=clock_flip" if matrix[3] < 0 else "transpose=cclock"
    if theta == 180:
        flips = [name for name, flipped in (("hflip", matrix[0] < 0), ("vflip", matrix[4] < 0)) if flipped]
    elif theta == 0:
        flips = ["vflip"] if matrix[4] < 0 else []
    else:
        return None
    if flips == ["vflip"]:
        flips.insert(0, "copy")
    return ",".join(flips) or None


def build_hdr10_zscale_chain(tonemap_algorithm: str, input_options: Sequence[str] = ()) -> str:
    """Return the software tone-map chain that turns HDR10 (or a DV base layer) into SDR yuv420p.

    Args:
        tonemap_algorithm: FFmpeg ``tonemap`` algorithm (e.g. ``hable``).
        input_options: Extra ``zscale`` input-colour options (``t=``, ``p=``, ``m=``) for streams whose
            tags are missing; they precede the linearisation step.

    Returns:
        The filter chain, without fps or scale filters.
    """
    zscale_input = ":".join([*input_options, "t=linear", "npl=100"])
    return (
        f"zscale={zscale_input},format=gbrpf32le,"
        f"zscale=p=bt709,tonemap={tonemap_algorithm}:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
    )


def build_dv5_vf(
    path_kind: str,
    tonemap_algorithm: str,
    fps_value: float,
    base_scale: str,
) -> str:
    """Return the ``-vf`` filter string for a DV Profile 5 thumbnail run.

    Three vendor variants share the same high-level shape:

        fps-drop → upload-to-GPU → tonemap → hwdownload → format → scale

    but differ in the GPU hop and tonemap filter:

    * ``opencl_dv5_intel``   – VAAPI → OpenCL hwmap, ``tonemap_opencl``
      (Jellyfin's DV-aware patch).  The only path that handles DV5 RPU on
      an Intel iGPU without spilling to libplacebo on a different device.
    * ``libplacebo_vaapi``   – VAAPI → Vulkan hwmap (DMA-BUF), libplacebo.
      Used for AMD Radeon and (historically) Intel before we moved Intel
      to OpenCL because Mesa ANV's Vulkan interop is broken on DV5.
    * ``libplacebo_dv5``     – CPU → Vulkan hwupload, libplacebo.  Used
      for NVIDIA (where CUDA→Vulkan interop is not available in FFmpeg)
      and for the software-decode + libplacebo retry fallback.

    ``fps`` is placed first across all variants, which is:
      * required on NVIDIA Turing (fps-inside-libplacebo exhausts the
        Vulkan image allocator with VK_ERROR_OUT_OF_DEVICE_MEMORY at
        4K p010 — see commit 70cba4e);
      * significantly faster on Intel OpenCL (17× vs 2.7× measured); and
      * RPU-safe because fps only drops frames on timestamps — it never
        touches pixel data or side-data.

    ``contrast=1.3, saturation=1.3`` on the libplacebo paths restore the
    punch that the default PQ→SDR curve leaves on the table (details in
    the callsite history; visually verified across MIB dark/mid/bright).

    Raises:
        ValueError: if ``path_kind`` is not a recognised DV5 variant.
    """
    fps = f"fps=fps={fps_value}:round=up"
    if path_kind == DV5_PATH_INTEL_OPENCL:
        return (
            f"{fps},"
            "setparams=color_primaries=bt2020:"
            "color_trc=smpte2084:colorspace=bt2020nc,"
            "hwmap=derive_device=opencl:mode=read,"
            f"tonemap_opencl=format=nv12:p=bt709:t=bt709:"
            f"m=bt709:tonemap={tonemap_algorithm}"
            f":peak=100:desat=0,"
            f"hwdownload,format=nv12,format=yuv420p,{base_scale}"
        )
    libplacebo_opts = f"libplacebo=tonemapping={tonemap_algorithm}:format=yuv420p:contrast=1.3:saturation=1.3"
    if path_kind == DV5_PATH_VAAPI_VULKAN:
        return f"{fps},hwmap=derive_device=vulkan,{libplacebo_opts},hwdownload,format=yuv420p,{base_scale}"
    if path_kind == DV5_PATH_LIBPLACEBO:
        return f"{fps},hwupload,{libplacebo_opts},hwdownload,format=yuv420p,{base_scale}"
    raise ValueError(f"Unknown DV5 path_kind: {path_kind!r}")
