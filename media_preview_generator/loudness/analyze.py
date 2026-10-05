"""Run Plex's loudnorm analysis of one audio stream and turn its report into Plex's ``ln:*`` fields."""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Callable

from loguru import logger

from ..markers.freeze import Freeze
from ..markers.probe import kill_and_collect

# The filter Plex Media Server 1.43 runs for each audio stream (``Plex Transcoder -i FILE -map 0:N -af ... -f null -``).
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1:LRA=9:print_format=json"
# Plex decodes EAC3 with its Dolby decoder (``-eae_prefix``, only inside Plex), which applies no dynamic range
# compression; ffmpeg's EAC3 decoder does unless told not to. AC3 goes through ffmpeg's decoder in Plex too, DRC on.
NO_DRC_CODECS = frozenset({"eac3"})
# The version Plex writes beside its fields (``ln:loudnessAnalysisVersion``).
ANALYSIS_VERSION = "0.02"
# The amd64 image's analysis-only FFmpeg (Dockerfile stage ``ffmpeg-loudnorm``): one jellyfin-ffmpeg8 package release
# built from its own source plus a loudnorm patch, so the same decoders and values for a fraction of the CPU.
FAST_DIR = "/usr/local/lib/ffmpeg-loudnorm"
JELLYFIN_FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
# loudnorm's report key → Plex's field.
_FIELDS = {
    "input_i": "ln:loudness",
    "input_tp": "ln:peak",
    "input_lra": "ln:lra",
    "input_thresh": "ln:threshold",
    "target_offset": "ln:gainOffset",
}
# A stalled mount must not hold a worker for ever; a long file still gets well past real time.
MIN_TIMEOUT_S = 600.0
MAX_TIMEOUT_S = 4 * 3600.0
_POLL_S = 0.5
KILL_WAIT_S = 5.0
REAPER = "loudness-reaper"


class LoudnessError(Exception):
    """ffmpeg failed, timed out, was cancelled, or printed no loudnorm report."""


class LoudnessTimeout(LoudnessError):
    """ffmpeg ran past its time limit."""


def command(ffmpeg: str, path: str, index: int, codec: str = "", progress: str = "") -> list[str]:
    """The ffmpeg command for one stream: Plex's own, on the CPU (audio decoding gains nothing from a GPU).

    Args:
        ffmpeg: The ffmpeg binary.
        path: The media file.
        index: The stream's index in the file (Plex's ``media_streams.index``).
        codec: The stream's codec (``media_streams.codec``); EAC3 is decoded without DRC, as Plex's decoder does.
        progress: Where ffmpeg writes its ``-progress`` blocks (``pipe:N``); empty: no progress output.

    Returns:
        The argument list.

    Raises:
        LoudnessError: The stream index is not a nonnegative integer.
    """
    if type(index) is not int or index < 0:
        raise LoudnessError("The audio stream index must be a nonnegative integer")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        *(["-progress", progress] if progress else []),
        *(["-drc_scale", "0"] if codec in NO_DRC_CODECS else []),
        "-i",
        path,
        "-map",
        f"0:{index}",
        "-af",
        LOUDNORM_FILTER,
        "-f",
        "null",
        "-",
    ]


def timeout_for(duration_ms: int | None) -> float:
    """Three times the file's length, kept between ``MIN_TIMEOUT_S`` and ``MAX_TIMEOUT_S``."""
    return min(MAX_TIMEOUT_S, max(MIN_TIMEOUT_S, 3 * (duration_ms or 0) / 1000))


def parse(stderr: str) -> dict[str, str]:
    """The loudnorm report: the last JSON object ffmpeg printed.

    Raises:
        LoudnessError: No report in the output.
    """
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end < start:
        raise LoudnessError("ffmpeg printed no loudnorm report")
    try:
        report = json.loads(stderr[start : end + 1])
    except ValueError as exc:
        raise LoudnessError("ffmpeg printed an unreadable loudnorm report") from exc
    return report


def ln_fields(report: dict[str, str]) -> dict[str, str]:
    """Plex's ``ln:*`` fields from a loudnorm report: its values as printed (two decimals), as Plex stores them.

    Raises:
        LoudnessError: A value is missing or differs from Plex's measured numeric/silence representation.
    """
    fields = {"ln:loudnessAnalysisVersion": ANALYSIS_VERSION}
    for key, name in _FIELDS.items():
        try:
            value = float(report[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise LoudnessError(f"loudnorm report lacks a usable {key}") from exc
        if math.isnan(value):
            raise LoudnessError(f"loudnorm measured no usable {key}")
        fields[name] = str(report[key])
    if not valid_measurements(fields):
        raise LoudnessError("loudnorm reported unsupported loudness measurements")
    return fields


def valid_measurements(fields: dict[str, str]) -> bool:
    """Recognize finite measurements and Plex's verified silent/very-short stream values.

    Plex 1.43.4 stores negative-infinite integrated loudness with positive-infinite
    gain offset for silence and audio too short for integrated measurement. A
    silent stream also has negative-infinite peak; its range and threshold stay finite.
    """
    try:
        values = {name: float(fields[name]) for name in _FIELDS.values()}
    except (KeyError, TypeError, ValueError):
        return False
    if all(math.isfinite(value) for value in values.values()):
        return True
    return (
        values["ln:loudness"] == -math.inf
        and values["ln:gainOffset"] == math.inf
        and (math.isfinite(values["ln:peak"]) or values["ln:peak"] == -math.inf)
        and math.isfinite(values["ln:lra"])
        and math.isfinite(values["ln:threshold"])
    )


def _report_progress(
    rfd: int, pending: bytes, duration_ms: int | None, progress: Callable[[float, str], None] | None
) -> bytes:
    """Read the -progress lines ffmpeg wrote so far; report the last complete position. Returns the unfinished line."""
    while True:
        try:
            chunk = os.read(rfd, 65536)
        except BlockingIOError:
            break
        if not chunk:
            break
        pending += chunk
    *lines, pending = pending.split(b"\n")
    values = dict(line.decode("ascii", "replace").partition("=")[::2] for line in lines if b"=" in line)
    out_us = values.get("out_time_us", "")
    if progress and out_us.isdigit():
        progress(min(1.0, int(out_us) / (duration_ms * 1000)) if duration_ms else 0.0, values.get("speed", "").strip())
    return pending


def run(
    ffmpeg: str,
    path: str,
    index: int,
    *,
    duration_ms: int | None,
    codec: str = "",
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | Freeze | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, str]:
    """Analyse one stream and return its ``ln:*`` fields.

    Args:
        ffmpeg: The ffmpeg binary.
        path: The media file (read only).
        index: The stream's index in the file.
        duration_ms: The file's length, for the time limit and the progress fraction.
        codec: The stream's codec (see ``command``).
        cancel_check: True once the job is cancelled; ffmpeg is killed.
        pause_check: True while everything is paused: ffmpeg is stopped where it is and the time limit moves out.
        progress: Called with the fraction of the stream analysed (0 to 1; 0 without a duration) and ffmpeg's speed
            (``17.5x``), as ffmpeg reports them.

    Raises:
        LoudnessError: ffmpeg failed, timed out, was cancelled, or its report was unusable.
    """
    name = os.path.basename(path)
    timeout_s = timeout_for(duration_ms)
    freeze = Freeze.of(pause_check)
    if cancel_check and cancel_check():
        raise LoudnessError(f"Loudness analysis of {name} cancelled")
    freeze.hold(cancel_check=cancel_check, name=name)
    if cancel_check and cancel_check():
        raise LoudnessError(f"Loudness analysis of {name} cancelled")
    # ffmpeg's -progress blocks (key=value lines) arrive on a pipe of their own; stderr keeps the loudnorm report.
    rfd, wfd = os.pipe()
    os.set_blocking(rfd, False)
    # Its own session, so a pause stops ffmpeg's whole group and never the app's.
    try:
        proc = subprocess.Popen(
            command(ffmpeg, path, index, codec, progress=f"pipe:{wfd}"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(wfd,),
        )
    except BaseException as exc:
        os.close(rfd)
        if isinstance(exc, OSError):
            raise LoudnessError(
                f"Could not start ffmpeg analysing {name}: {exc.strerror or type(exc).__name__}"
            ) from exc
        raise
    finally:
        os.close(wfd)
    deadline = freeze.clock() + timeout_s
    pending = b""
    try:
        while True:
            try:
                _out, err = proc.communicate(timeout=_POLL_S)
                break
            except subprocess.TimeoutExpired:
                pending = _report_progress(rfd, pending, duration_ms, progress)
                freeze.hold(proc, cancel_check=cancel_check, name=name)
                cancelled = bool(cancel_check and cancel_check())
                if cancelled or freeze.clock() > deadline:
                    if cancelled:
                        raise LoudnessError(f"Loudness analysis of {name} cancelled") from None
                    raise LoudnessTimeout(f"Loudness analysis of {name} timed out after {timeout_s:.0f} s") from None
        _report_progress(rfd, pending, duration_ms, progress)  # what ffmpeg wrote after the last poll
    except BaseException:
        kill_and_collect(proc, what=f"ffmpeg analysing loudness of {name}", reaper_name=REAPER, wait_s=KILL_WAIT_S)
        raise
    finally:
        os.close(rfd)
    if cancel_check and cancel_check():
        raise LoudnessError(f"Loudness analysis of {name} cancelled")
    text = (err or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise LoudnessError(f"ffmpeg exited {proc.returncode} analysing {name} stream {index}: {text.strip()[-200:]}")
    return ln_fields(parse(text))


_twins: dict[str, str | None] = {}


def _installed_jellyfin() -> str:
    """The installed jellyfin-ffmpeg8 package version (``8.1.3-1-noble``)."""
    return subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", "jellyfin-ffmpeg8"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()


def fast_twin(ffmpeg: str) -> str | None:
    """The fast build when ``ffmpeg`` is the jellyfin-ffmpeg package release it was built from, else None."""
    if ffmpeg in _twins:
        return _twins[ffmpeg]
    binary = os.path.join(FAST_DIR, "ffmpeg")
    if ffmpeg != JELLYFIN_FFMPEG or not os.access(binary, os.X_OK):
        _twins[ffmpeg] = None
        return None
    try:
        with open(os.path.join(FAST_DIR, "jellyfin-release"), encoding="utf-8") as f:
            built = f.read().strip()
        installed = _installed_jellyfin()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("Fast loudness ffmpeg not used for now: {}", exc)  # not cached: the next stream asks again
        return None
    same = installed == built or installed.startswith(built + "-")
    if not same:
        logger.info("Fast loudness ffmpeg not used: built from jellyfin-ffmpeg {}, {} installed", built, installed)
    _twins[ffmpeg] = binary if same else None
    return _twins[ffmpeg]


def measure(ffmpeg: str, path: str, index: int, **kwargs) -> dict[str, str]:
    """``run`` with the fast twin of ``ffmpeg`` when there is one; a stream it fails on runs again with ``ffmpeg``.

    A timeout is not retried: the source stalled, and a second full time limit would only hold the worker longer.
    """
    fast = fast_twin(ffmpeg)
    if fast:
        try:
            return run(fast, path, index, **kwargs)
        except LoudnessTimeout:
            raise
        except LoudnessError as exc:
            cancel_check = kwargs.get("cancel_check")
            if cancel_check and cancel_check():
                raise
            logger.warning("Fast loudness analysis failed ({}); retrying with {}", exc, ffmpeg)
    return run(ffmpeg, path, index, **kwargs)
