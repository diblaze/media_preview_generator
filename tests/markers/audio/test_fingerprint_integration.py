"""Real ffmpeg: a generated 60 s stereo tone file fingerprints to ~window/POINT_S points, also past an audio stream ffmpeg
has no decoder for."""

import shutil
import subprocess

import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg, compute_fingerprint, window_s

pytestmark = pytest.mark.integration


def test_real_ffmpeg_fingerprint_length(tmp_path):
    ffmpeg = chromaprint_ffmpeg(shutil.which("ffmpeg"))
    if ffmpeg is None:
        pytest.skip("no ffmpeg with chromaprint")
    media = tmp_path / "tone.mka"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "anoisesrc=color=pink:seed=7:d=60", "-ac", "2", str(media)],
        check=True,
    )
    points = compute_fingerprint(str(media), 60_000, ffmpeg=ffmpeg)
    # Chromaprint emits nothing until its FFT frame and filter context are full (~2.5 s), so a window yields a
    # near-constant ~20 points fewer than window/POINT_S (21 short for both 21 s and 42 s windows on ffmpeg 8.0.1).
    expected = window_s(60_000) / POINT_S
    assert expected - 30 < len(points) <= expected


def test_real_ffmpeg_fingerprints_past_an_audio_stream_it_has_no_decoder_for(tmp_path):
    # Web releases muxing AV3A (no ffmpeg decoder) ahead of an EAC3 failed every episode's fingerprint with exit 234.
    ffmpeg = chromaprint_ffmpeg(shutil.which("ffmpeg"))
    if ffmpeg is None:
        pytest.skip("no ffmpeg with chromaprint")
    media = tmp_path / "two.mp4"
    noise = "anoisesrc=color=pink:seed=7:d=60"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", noise, "-f", "lavfi", "-i", noise, "-map", "0", "-map", "1",
         "-ac:a:0", "6", "-ac:a:1", "2", "-c:a", "aac", str(media)],
        check=True,
    )  # fmt: skip
    data = bytearray(media.read_bytes())
    entry = data.find(b"mp4a")  # the first track's sample entry: renamed to AV3A and its codec config hidden
    data[entry : entry + 4] = b"av3a"
    config = data.find(b"esds", entry)
    data[config : config + 4] = b"free"
    av3a = tmp_path / "av3a.mp4"
    av3a.write_bytes(bytes(data))
    plain = subprocess.run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(av3a), "-t", "5", "-vn", "-f", "chromaprint", "-"],
        capture_output=True,
    )
    assert plain.returncode != 0 and b"no decoder found" in plain.stderr  # the failure the retry is for

    points = compute_fingerprint(str(av3a), 60_000, ffmpeg=ffmpeg)
    expected = window_s(60_000) / POINT_S
    assert expected - 30 < len(points) <= expected
