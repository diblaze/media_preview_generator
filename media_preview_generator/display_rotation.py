"""Reading a video stream's display matrix (its rotation and mirroring) and the filters that turn its frames upright.

A leaf module, standard library only: the Plex database node imports the markers probe, which uses it, without the
preview pipeline's dependencies.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


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
