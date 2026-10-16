"""
Tests for media_processing.py module.

Tests BIF generation, FFmpeg execution, progress parsing, path mapping,
HDR detection, and the complete processing pipeline.
"""

import json
import os
import shutil
import struct
import subprocess
from unittest.mock import MagicMock, mock_open, patch

import pytest
from loguru import logger

from media_preview_generator.gpu import VulkanProbeResult
from media_preview_generator.processing import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
    FFMPEG_STALL_TIMEOUT_SEC,
    CancellationError,
    CodecNotSupportedError,
    _detect_codec_error,
    _detect_hwaccel_runtime_error,
    _diagnose_ffmpeg_exit_code,
    _save_ffmpeg_failure_log,
    _verify_tmp_folder_health,
    build_dv5_vf,
    clear_failures,
    failure_scope,
    generate_bif,
    generate_images,
    get_failures,
    parse_ffmpeg_progress_line,
    record_failure,
)
from media_preview_generator.processing.generator import _video_details_from_ffprobe, read_media_info
from media_preview_generator.processing.hdr_detection import (
    detect_dolby_vision_rpu_error as _detect_dolby_vision_rpu_error,
)
from media_preview_generator.processing.hdr_detection import (
    detect_zscale_colorspace_error as _detect_zscale_colorspace_error,
)
from media_preview_generator.processing.hdr_detection import is_dolby_vision as _is_dolby_vision
from media_preview_generator.processing.hdr_detection import is_dv_no_backward_compat as _is_dv_no_backward_compat
from media_preview_generator.processing.hdr_detection import is_hdr_transfer


@pytest.fixture(autouse=True)
def _safe_keyframe_probe(monkeypatch):
    """Patch the issue-#238 keyframe probe + duplicate-check to safe
    no-op defaults for the duration of every test in this file.

    The probe and the post-extract validator were added in the fix for
    duplicate-thumbnail bugs on files whose keyframe spacing exceeds
    the user's thumbnail interval.  In production they call ffprobe
    and hash JPGs on disk; in these tests neither is realistic — the
    video path is fake and ``subprocess.run`` is a blanket MagicMock.
    Letting the real probe run here would always return ``None``
    (unsafe) and force the no-skip-frame retry path, breaking every
    assertion about the first attempt's command shape.

    The dedicated probe behaviour itself is covered in
    ``test_processing_keyframe_probe.py``.
    """
    from media_preview_generator.processing import generator as _gen

    monkeypatch.setattr(_gen, "_probe_max_keyframe_gap", lambda *_a, **_kw: 0.5)
    monkeypatch.setattr(_gen, "_has_duplicate_thumbnails", lambda *_a, **_kw: False)


class TestBIFGeneration:
    """Test BIF file generation."""

    def test_generate_bif_creates_valid_structure(self, temp_dir, mock_config):
        """Test that BIF file has correct binary structure."""
        # Create test thumbnails
        for i in range(3):
            timestamp = i * 5
            img_path = os.path.join(temp_dir, f"{timestamp:010d}.jpg")
            with open(img_path, "wb") as f:
                f.write(b"\xff\xd8\xff")

        # Generate BIF
        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        # Verify BIF file exists
        assert os.path.exists(bif_path)

        # Verify BIF magic bytes
        with open(bif_path, "rb") as f:
            magic = list(f.read(8))
            assert magic == [0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A]

            # Check version
            version = struct.unpack("<I", f.read(4))[0]
            assert version == 0

            # Check image count
            image_count = struct.unpack("<I", f.read(4))[0]
            assert image_count == 3

            # Check frame interval (5 seconds = 5000ms)
            frame_interval = struct.unpack("<I", f.read(4))[0]
            assert frame_interval == 5000

    def test_generate_bif_index_table(self, temp_dir, mock_config):
        """Test that BIF index table has correct offsets."""
        # Create thumbnails with known sizes
        thumbnail_sizes = []
        for i in range(5):
            timestamp = i * 5
            img_path = os.path.join(temp_dir, f"{timestamp:010d}.jpg")
            data = b"\xff\xd8\xff" + (b"X" * (100 * (i + 1)))
            with open(img_path, "wb") as f:
                f.write(data)
            thumbnail_sizes.append(len(data))

        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        # Verify index table
        with open(bif_path, "rb") as f:
            f.seek(64)  # Start of index table

            # Check each index entry
            expected_offset = 64 + (8 * 6)  # Header + index table (5 entries + end marker)
            for timestamp in range(5):
                ts = struct.unpack("<I", f.read(4))[0]
                offset = struct.unpack("<I", f.read(4))[0]

                assert ts == timestamp
                assert offset == expected_offset
                expected_offset += thumbnail_sizes[timestamp]

            # Check end marker
            end_ts = struct.unpack("<I", f.read(4))[0]
            end_offset = struct.unpack("<I", f.read(4))[0]
            assert end_ts == 0xFFFFFFFF
            assert end_offset == expected_offset

    def test_generate_bif_embedded_images(self, temp_dir, mock_config):
        """Test that actual image data is embedded in BIF."""
        # Create thumbnail with recognizable content
        test_data = b"\xff\xd8\xff" + b"TEST_IMAGE_DATA_12345"
        img_path = os.path.join(temp_dir, "0000000000.jpg")
        with open(img_path, "wb") as f:
            f.write(test_data)

        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        # Verify image data is embedded
        with open(bif_path, "rb") as f:
            content = f.read()
            assert b"TEST_IMAGE_DATA_12345" in content
            assert test_data in content

    def test_generate_bif_empty_directory(self, temp_dir, mock_config):
        """No thumbnails, no BIF: Plex and Emby would take an empty one as a finished preview."""
        bif_path = os.path.join(temp_dir, "empty.bif")
        with pytest.raises(ValueError, match="no thumbnails"):
            generate_bif(bif_path, temp_dir, mock_config)

        assert not os.path.exists(bif_path)

    def test_generate_bif_frame_interval(self, temp_dir, mock_config):
        """Test that frame interval is correctly converted to milliseconds."""
        # Test with 10 second interval
        mock_config.plex_bif_frame_interval = 10

        img_path = os.path.join(temp_dir, "0000000000.jpg")
        with open(img_path, "wb") as f:
            f.write(b"\xff\xd8\xff")

        bif_path = os.path.join(temp_dir, "test.bif")
        generate_bif(bif_path, temp_dir, mock_config)

        with open(bif_path, "rb") as f:
            f.seek(16)  # Skip magic + version + count
            frame_interval = struct.unpack("<I", f.read(4))[0]
            assert frame_interval == 10000  # 10 seconds = 10000ms


class TestFFmpegProgressParsing:
    """Test FFmpeg progress line parsing."""

    def test_parse_ffmpeg_progress_line_duration(self):
        """Test parsing Duration line from FFmpeg."""
        line = "  Duration: 01:23:45.67, start: 0.000000, bitrate: 8000 kb/s"
        duration = parse_ffmpeg_progress_line(line, 0.0)

        # 1 hour + 23 minutes + 45.67 seconds
        expected = 3600 + (23 * 60) + 45.67
        assert abs(duration - expected) < 0.1

    def test_parse_ffmpeg_progress_line_progress(self):
        """Test parsing progress line with time=."""
        line = "frame= 1234 fps=45.6 q=28.0 size=  12345kB time=00:12:34.56 bitrate= 123.4kbits/s speed=1.23x"

        callback_data = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining=None,
            frame=0,
            fps=0,
            q=0,
            size=0,
            time_str="",
            bitrate=0,
        ):
            callback_data["progress"] = progress
            callback_data["current"] = current
            callback_data["speed"] = speed
            callback_data["frame"] = frame
            callback_data["fps"] = fps
            callback_data["time_str"] = time_str

        total_duration = 1800.0  # 30 minutes
        parse_ffmpeg_progress_line(line, total_duration, callback)

        # Verify callback was called with correct data
        assert "progress" in callback_data
        assert callback_data["frame"] == 1234
        assert abs(callback_data["fps"] - 45.6) < 0.1
        assert callback_data["speed"] == "1.23x"
        assert callback_data["time_str"] == "00:12:34.56"

    def test_parse_ffmpeg_progress_line_with_callback(self):
        """Test that progress callback is invoked correctly."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=1.0x"

        callback_called = False

        def callback(*args, **kwargs):
            nonlocal callback_called
            callback_called = True

        parse_ffmpeg_progress_line(line, 100.0, callback)
        assert callback_called

    def test_parse_ffmpeg_progress_line_progress_decimal_precision(self):
        """Progress percent uses one decimal place for UI (Issue #144)."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:33.33 bitrate= 800.0kbits/s speed=1.0x"
        progress_seen = []

        def callback(
            progress,
            current,
            total,
            speed,
            remaining=None,
            frame=0,
            fps=0,
            q=0,
            size=0,
            time_str="",
            bitrate=0,
        ):
            progress_seen.append(progress)

        parse_ffmpeg_progress_line(line, 100.0, callback)
        assert len(progress_seen) == 1
        # 33.33/100*100 = 33.33 -> round(33.33, 1) = 33.3
        assert progress_seen[0] == 33.3
        assert isinstance(progress_seen[0], float)

    def test_parse_ffmpeg_progress_line_no_callback(self):
        """Test parsing without callback doesn't crash."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=1.0x"
        result = parse_ffmpeg_progress_line(line, 100.0, None)
        assert result == 100.0

    def test_remaining_time_accounts_for_speed(self):
        """remaining_time should be wall-clock ETA, not raw media remaining."""
        line = "frame= 5000 fps=120 q=28.0 size=  50000kB time=00:05:00.00 bitrate= 1000.0kbits/s speed=100.0x"
        captured = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining,
            frame,
            fps,
            q,
            size,
            time_str,
            bitrate,
        ):
            captured["remaining"] = remaining
            captured["speed"] = speed

        total_duration = 600.0  # 10 minutes
        parse_ffmpeg_progress_line(line, total_duration, callback)

        # current_time = 300s, remaining media = 300s, speed = 100x
        # wall-clock ETA = 300 / 100 = 3 seconds
        assert captured["speed"] == "100.0x"
        assert abs(captured["remaining"] - 3.0) < 0.1

    def test_remaining_time_at_1x_speed(self):
        """At 1x speed, wall-clock ETA equals remaining media duration."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=1.0x"
        captured = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining,
            frame,
            fps,
            q,
            size,
            time_str,
            bitrate,
        ):
            captured["remaining"] = remaining

        parse_ffmpeg_progress_line(line, 100.0, callback)
        # remaining media = 90s, speed = 1x -> wall-clock = 90s
        assert abs(captured["remaining"] - 90.0) < 0.1

    def test_remaining_time_no_speed_falls_back(self):
        """When speed is not parseable, remaining_time falls back to raw media remaining."""
        line = "frame= 100 fps=30.0 q=28.0 size=  1000kB time=00:00:10.00 bitrate= 800.0kbits/s speed=N/Ax"
        captured = {}

        def callback(
            progress,
            current,
            total,
            speed,
            remaining,
            frame,
            fps,
            q,
            size,
            time_str,
            bitrate,
        ):
            captured["remaining"] = remaining

        parse_ffmpeg_progress_line(line, 100.0, callback)
        # speed not parseable -> falls back to raw remaining = 90s
        assert abs(captured["remaining"] - 90.0) < 0.1


class TestDetectCodecError:
    """Test codec error detection for CPU fallback."""

    def test_detect_codec_error_stderr_patterns(self):
        """Test detection of codec error patterns in stderr."""
        # Test various codec error patterns
        patterns = [
            ["Codec not supported"],
            ["Unsupported codec with id 123"],
            ["Unknown decoder 'av1'"],
            ["Decoder not found for codec"],
            ["Could not find codec"],
            ["No decoder for codec av1"],
        ]

        for stderr_lines in patterns:
            result = _detect_codec_error(1, stderr_lines)
            assert result is True, f"Should detect codec error in: {stderr_lines}"

    def test_detect_codec_error_exit_code_69(self):
        """Test detection via exit code 69 (max error rate)."""
        stderr_lines = ["Some generic error message"]
        result = _detect_codec_error(69, stderr_lines)
        assert result is True

    def test_detect_codec_error_exit_code_minus22(self):
        """Test detection via exit code -22 (EINVAL)."""
        stderr_lines = ["Some error"]
        result = _detect_codec_error(-22, stderr_lines)
        assert result is True

    def test_detect_codec_error_exit_code_234(self):
        """Test detection via exit code 234 (wrapped -22 on Unix)."""
        stderr_lines = ["Some error"]
        result = _detect_codec_error(234, stderr_lines)
        assert result is True

    def test_detect_codec_error_no_match(self):
        """Test that non-codec errors are not detected."""
        stderr_lines = ["File not found", "Permission denied"]
        result = _detect_codec_error(1, stderr_lines)
        assert result is False

    def test_detect_codec_error_success_exit(self):
        """Test that stderr patterns are checked even with success exit code."""
        # Even if exit code is 0, if stderr contains codec error, it should be detected
        stderr_lines = ["Unsupported codec"]
        result = _detect_codec_error(0, stderr_lines)
        assert result is True  # Stderr pattern takes precedence

    def test_detect_codec_error_success_exit_no_codec_error(self):
        """Test that success exit code doesn't trigger detection when no codec error in stderr."""
        stderr_lines = ["File processed successfully"]
        result = _detect_codec_error(0, stderr_lines)
        assert result is False

    def test_detect_codec_error_case_insensitive(self):
        """Test that detection is case-insensitive."""
        stderr_lines = ["UNSUPPORTED CODEC", "Codec Not Supported", "unsupported codec"]
        for line in stderr_lines:
            result = _detect_codec_error(1, [line])
            assert result is True, f"Should detect codec error case-insensitively: {line}"


class TestDetectHwaccelRuntimeError:
    """Test GPU hardware accelerator runtime error detection."""

    def test_detect_vaapi_surface_sync_error(self):
        """Test detection of VAAPI surface sync failures (the exact crash from issue)."""
        stderr_lines = [
            "[AVHWFramesContext @ 0x146e00045d00] Failed to sync surface 0: 23 (internal decoding error).",
            "[h264 @ 0x55a542e23fc0] Failed to transfer data to output frame: -5.",
        ]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_transfer_data_error(self):
        """Test detection of hw frame transfer failure."""
        stderr_lines = ["[h264 @ 0x55a5] Failed to transfer data to output frame: -5."]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_avhwframescontext_error(self):
        """Test detection of generic AVHWFramesContext messages."""
        stderr_lines = ["[AVHWFramesContext @ 0xabc] Some unexpected error occurred"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_no_match_on_avhwframescontext_debug_chatter(self):
        """A working VAAPI run at -loglevel debug names AVHWFramesContext on every surface it creates."""
        stderr_lines = [
            "[AVHWFramesContext @ 0x55d0c8a1b2c0] Created surface 0x4000000.",
            "[AVHWFramesContext @ 0x55d0c8a1b2c0] Direct mapping possible.",
            "[in @ 0x55d0c8a1c100] Error reading input: Input/output error",
        ]
        assert _detect_hwaccel_runtime_error(stderr_lines) is False

    def test_detect_cuda_error(self):
        """Test detection of CUDA decode errors."""
        stderr_lines = ["CUDA error: out of memory"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_cuvid_error(self):
        """Test detection of cuvid decode errors."""
        stderr_lines = ["cuvid decode error on frame 1234"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_failed_to_create_surface(self):
        """Test detection of surface creation failures."""
        stderr_lines = ["[vaapi @ 0x55a5] Failed to create surface"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    def test_detect_hwaccel_init_error(self):
        """Test detection of hwaccel initialisation errors."""
        stderr_lines = ["hwaccel initialisation returned error"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    @pytest.mark.parametrize(
        "line",
        [
            "[vist#0:0/h264 @ 0x630c] [dec:h264 @ 0x630c] Hardware device setup failed for decoder: Generic error in an "
            "external library",
            "Device setup failed for decoder on input stream #0:0 : Generic error in an external library",
        ],
        ids=["ffmpeg-7-and-later", "older-ffmpeg"],
    )
    def test_detect_a_gpu_that_cant_start_the_decode(self, line):
        """A container that lost its GPU: the decoder's GPU device can't be set up, and nothing decodes."""
        assert _detect_hwaccel_runtime_error([line]) is True

    def test_no_match_on_normal_errors(self):
        """Test that non-hwaccel errors are not detected."""
        stderr_lines = [
            "File not found",
            "Permission denied",
            "Conversion failed!",
            "Error while processing the decoded data",
        ]
        assert _detect_hwaccel_runtime_error(stderr_lines) is False

    def test_no_match_on_empty_lines(self):
        """Test that empty input returns False."""
        assert _detect_hwaccel_runtime_error([]) is False
        assert _detect_hwaccel_runtime_error(None) is False

    def test_case_insensitive_matching(self):
        """Test that patterns are matched case-insensitively."""
        stderr_lines = ["FAILED TO SYNC SURFACE 0: 23"]
        assert _detect_hwaccel_runtime_error(stderr_lines) is True

    @pytest.mark.parametrize(
        "line",
        [
            "[tonemap @ 0x55a5] Failed to enqueue kernel: -5.",
            "[hwmap @ 0x55a5] Failed to create CL image from surface 0x3 plane 0: -34.",
            "[AVHWDeviceContext @ 0x55a5] OpenCL error -5 while mapping frame",
            "FAILED TO ENQUEUE KERNEL: -5",
        ],
        ids=["enqueue-kernel", "cl-image", "opencl-error", "upper-case"],
    )
    def test_detect_opencl_filter_errors(self, line):
        """Intel and AMD tone-map Dolby Vision in OpenCL: its kernel and image errors are GPU errors (often exit 251)."""
        assert _detect_hwaccel_runtime_error([line]) is True

    def test_no_match_on_opencl_debug_chatter(self):
        """At -loglevel debug a working OpenCL run names OpenCL on many lines; none of them is an error."""
        stderr_lines = [
            "[OpenCL @ 0x5617a0] 1 OpenCL platforms found.",
            '[OpenCL @ 0x5617a0] 1 OpenCL devices found on platform "Intel(R) OpenCL Graphics".',
            "[OpenCL @ 0x5617a0] 0.0: Intel(R) OpenCL Graphics / Intel(R) Arc(TM) A380 Graphics",
            "[OpenCL @ 0x5617a0] The cl_intel_va_api_media_sharing extension is required for QSV to OpenCL mapping.",
            "[OpenCL @ 0x5617a0] Maximum supported image size 16384x16384.",
            "Filter 'Parsed_tonemap_opencl_2' formats:",
            "  out[0] 'default': opencl",
            "[tonemap_opencl @ 0x7f3a10] Filter input: opencl, 3840x2160 (0).",
        ]
        assert _detect_hwaccel_runtime_error(stderr_lines) is False


class TestGenerateImages:
    """Test thumbnail generation with FFmpeg."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    def test_generate_images_calls_ffmpeg(
        self,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that generate_images calls FFmpeg with correct arguments."""
        # Mock heuristic check
        mock_run.return_value = MagicMock(returncode=0)

        # Mock MediaInfo
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # Mock FFmpeg process
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        # Mock file operations
        mock_exists.return_value = False

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        # Verify FFmpeg was called with the configured binary AND a
        # well-formed invocation — i.e. ``-i <source>`` (not just the
        # source path floating in argv) and an output destination
        # written into temp_dir. A regression that builds a malformed
        # command (e.g. forgets ``-i`` or points the output elsewhere)
        # would silently pass with the prior `_path in args` check.
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert args[0] == mock_config.ffmpeg_path, f"FFmpeg binary should be argv[0]; got {args[0]!r} (full: {args!r})"
        assert "-i" in args, f"FFmpeg invocation missing -i: {args!r}"
        i_index = args.index("-i")
        assert args[i_index + 1] == "/test/video.mp4", f"-i must be followed by source path; got {args[i_index + 1]!r}"
        # Output target must point under the requested temp_dir so a
        # regression that writes to a system location is caught.
        output_arg = args[-1]
        assert temp_dir in output_arg, f"FFmpeg output {output_arg!r} not under {temp_dir!r}"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("media_preview_generator.processing.ffmpeg_runner.time")
    @patch("media_preview_generator.processing.generator.time")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("glob.glob")
    def test_ffmpeg_stall_timeout_kills_process(
        self,
        mock_glob,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_time,
        mock_runner_time,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """When FFmpeg produces no progress for FFMPEG_STALL_TIMEOUT_SEC, process is killed."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # Process never exits on its own; we kill it via stall timeout
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.returncode = -9  # SIGKILL after kill()
        mock_popen.return_value = mock_proc

        # No progress file → last_progress_time never updated → stall detected
        mock_exists.return_value = False
        mock_glob.return_value = []

        # time.time() is called per _run_ffmpeg: start_local, last_progress_time, stall check, end_local.
        # generate_images retries without skip_frame after first failure, so we need values for 2 runs.
        # The stall check lives in ``ffmpeg_runner._run_ffmpeg``; the
        # wrapper timing uses ``media_processing.time``.  Both modules
        # get the same monotonically-advancing mock so the stall branch
        # fires on the third ``time.time()`` call of each run.
        stall_time = 0 + FFMPEG_STALL_TIMEOUT_SEC + 1
        one_run = [0, 0, stall_time, stall_time + 1]
        mock_time.time.side_effect = one_run + one_run
        mock_time.sleep.return_value = None
        mock_time.time_ns.return_value = 0  # for temp output filename
        mock_runner_time.time.side_effect = one_run + one_run
        mock_runner_time.sleep.return_value = None

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video.mp4", temp_dir, None, None, mock_config
        )

        assert success is False
        # The stall ends the cascade: no rerun without skip_frame on the same stuck mount
        assert mock_proc.kill.call_count == 1
        assert mock_proc.wait.call_count == 1

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_gpu_nvidia(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test NVIDIA GPU arguments are added."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda", mock_config)

        args = mock_popen.call_args[0][0]
        # -hwaccel must be IMMEDIATELY followed by "cuda" — checking
        # that both tokens appear independently would pass even if the
        # order were reversed (FFmpeg silently ignores trailing args
        # before -i, masking the bug).
        assert "-hwaccel" in args, f"NVIDIA path missing -hwaccel: {args!r}"
        assert args[args.index("-hwaccel") + 1] == "cuda", (
            f"-hwaccel must be followed by 'cuda'; got {args[args.index('-hwaccel') + 1]!r}"
        )
        # Generic "cuda" (no index suffix) must NOT add -hwaccel_device.
        assert "-hwaccel_device" not in args
        # And the source must still be wired through -i — the GPU
        # branch must not accidentally clobber the input.
        assert "-i" in args and args[args.index("-i") + 1] == "/test/video.mp4"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_gpu_nvidia_indexed_device(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """cuda:N device path emits -hwaccel_device N (issue #221)."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda:1", mock_config)

        args = mock_popen.call_args[0][0]
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-hwaccel_device" in args
        assert args[args.index("-hwaccel_device") + 1] == "1"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_gpu_amd(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test AMD VAAPI arguments are added."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, "AMD", "/dev/dri/renderD128", mock_config)

        args = mock_popen.call_args[0][0]
        # Mirror the NVIDIA test (line ~581): assert adjacency rather than
        # bare presence. A token-reorder regression that drops -hwaccel but
        # leaves "vaapi" floating in argv would silently pass the old
        # "vaapi" in args check (e.g. via -init_hw_device vaapi=...).
        assert "-hwaccel" in args, f"AMD path missing -hwaccel: {args!r}"
        assert args[args.index("-hwaccel") + 1] == "vaapi", (
            f"-hwaccel must be followed by 'vaapi'; got {args[args.index('-hwaccel') + 1]!r}"
        )
        # The SUT (ffmpeg_runner.py) uses the modern -hwaccel_device
        # (NOT the deprecated -vaapi_device) to carry the device path.
        assert "-hwaccel_device" in args, f"AMD path missing -hwaccel_device: {args!r}"
        assert args[args.index("-hwaccel_device") + 1] == "/dev/dri/renderD128", (
            f"-hwaccel_device must be followed by the render path; got {args[args.index('-hwaccel_device') + 1]!r}"
        )
        # And the source must still flow through -i — the GPU branch must
        # not accidentally clobber the input wiring.
        assert "-i" in args and args[args.index("-i") + 1] == "/test/video.mp4"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_cpu_only(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test CPU-only processing without hwaccel."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        args = mock_popen.call_args[0][0]
        # CPU-only path: gpu=None should NOT enable any hardware decoder.
        # Hardware-decoder flags are -hwaccel <type> with type in
        # {cuda, vaapi, qsv, videotoolbox, drm, vulkan}. Asserting absence
        # explicitly catches a regression that silently wires CUDA into the
        # CPU branch — the "if/pass" we replaced let that pass forever.
        gpu_decoders = {"cuda", "vaapi", "qsv", "videotoolbox", "drm", "vulkan", "auto"}
        for i, a in enumerate(args):
            if a == "-hwaccel" and i + 1 < len(args):
                assert args[i + 1] not in gpu_decoders, (
                    f"CPU-only generate_images() invoked GPU decoder {args[i + 1]!r} via -hwaccel"
                )

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_hdr_detection(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test HDR video uses correct filter chain."""
        mock_run.return_value = MagicMock(returncode=0)

        # Mock HDR video
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        args = mock_popen.call_args[0][0]
        # Find the -vf argument
        vf_index = args.index("-vf")
        vf_value = args[vf_index + 1]

        # zscale → tonemap MUST appear in that order in the filter
        # graph. A reversed chain (tonemap before zscale) silently
        # produces wrong colours because tonemap expects linear-light
        # input that zscale is responsible for producing. The previous
        # form of this test (independent presence checks) would pass
        # even with the chain reversed.
        assert "zscale" in vf_value, f"HDR path missing zscale: {vf_value!r}"
        assert "tonemap" in vf_value, f"HDR path missing tonemap: {vf_value!r}"
        zscale_pos = vf_value.find("zscale")
        tonemap_pos = vf_value.find("tonemap")
        assert zscale_pos < tonemap_pos, (
            f"HDR filter chain order wrong: zscale must come before tonemap; got {vf_value!r}"
        )
        # Tonemap must specify an algorithm — a bare ``tonemap`` with
        # no operator falls back to FFmpeg's default which differs
        # across versions; pin the contract.
        assert "tonemap=" in vf_value, f"tonemap must specify an algorithm (e.g. tonemap=hable); got {vf_value!r}"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.rename")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_generate_images_renames_files(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that images are renamed from img-XXXXXX.jpg to timestamp.jpg."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False

        # Mock glob to return test images
        mock_glob.return_value = [
            f"{temp_dir}/img-000001.jpg",
            f"{temp_dir}/img-000002.jpg",
            f"{temp_dir}/img-000003.jpg",
        ]

        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        # Verify rename was called once per frame, mapping img-NNNNNN.jpg to
        # the seconds-based name the BIF packer expects:
        #   img-000001.jpg (frame 0) -> 0000000000.jpg (0 seconds)
        #   img-000002.jpg (frame 1) -> 0000000005.jpg (5 seconds)
        #   img-000003.jpg (frame 2) -> 0000000010.jpg (10 seconds)
        calls = mock_rename.call_args_list
        assert len(calls) == 3
        renamed_targets = [os.path.basename(c.args[1]) for c in calls]
        assert renamed_targets == ["0000000000.jpg", "0000000005.jpg", "0000000010.jpg"]

    def test_generate_images_accepts_progress_callback_signature(self, mock_config):
        """generate_images must accept a 6th positional ``progress_callback``
        argument (the dispatcher passes one in real workloads). This test
        REPLACED a previous version that called the function with mocks too
        thin to actually drive the FFmpeg progress path; the old version
        had NO assertion at all and a comment admitting "may not be called".

        Here we verify the public contract via inspection — the call signature
        must match what the dispatcher relies on. The actual progress-parsing
        path is exercised end-to-end by tests/integration/test_e2e_*."""
        import inspect

        sig = inspect.signature(generate_images)
        params = list(sig.parameters.values())
        # Param order from production: video_file, output_folder, gpu, gpu_device,
        # config, progress_callback (optional). At least 5 required + 1 optional.
        assert len(params) >= 6, f"generate_images signature changed: {params!r}"
        # progress_callback must be a positional-or-keyword parameter (not a
        # keyword-only / **kwargs catch-all) so the dispatcher's positional
        # invocation keeps working.
        cb_param = params[5]
        assert cb_param.name == "progress_callback", f"6th param is {cb_param.name!r}, expected 'progress_callback'"
        assert cb_param.kind in (cb_param.POSITIONAL_OR_KEYWORD, cb_param.POSITIONAL_ONLY)

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_raises_codec_error_in_gpu_context(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that CodecNotSupportedError is raised when GPU fails with codec error."""
        # Mock heuristic check - returns True to use skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        # Mock MediaInfo
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # First GPU attempt with skip_frame fails with codec error
        mock_proc_gpu_skip = MagicMock()
        mock_proc_gpu_skip.poll.side_effect = [None, 0]
        mock_proc_gpu_skip.returncode = 69  # Max error rate

        # Retry GPU attempt without skip_frame also fails
        mock_proc_gpu_noskip = MagicMock()
        mock_proc_gpu_noskip.poll.side_effect = [None, 0]
        mock_proc_gpu_noskip.returncode = 69

        mock_popen.side_effect = [mock_proc_gpu_skip, mock_proc_gpu_noskip]
        mock_exists.return_value = False

        # Glob returns empty (no images produced)
        mock_glob.return_value = []

        # Mock codec error detection
        mock_detect.return_value = True

        # Enable CPU threads (but generate_images should raise exception, not do fallback)
        mock_config.cpu_threads = 1

        # Should raise CodecNotSupportedError instead of doing CPU fallback
        with pytest.raises(CodecNotSupportedError) as exc_info:
            generate_images("/test/video.mp4", temp_dir, "NVIDIA", None, mock_config)

        assert "GPU processing failed" in str(exc_info.value)
        # Codec-error detection ran on the failing FFmpeg process — that's
        # the predicate that gates whether to raise CodecNotSupportedError
        # vs fall back to CPU. Once is enough; more would mean the retry
        # path is mis-checking.
        assert mock_detect.call_count >= 1
        assert mock_popen.call_count == 2  # GPU with skip_frame + GPU without skip_frame (no CPU fallback)

        # Verify cleanup was attempted at least once for the partial output dir.
        assert mock_remove.call_count >= 1

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_no_cpu_fallback_when_disabled(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that CPU fallback is skipped when CPU threads = 0."""
        # Mock heuristic check - returns True to use skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # First attempt with skip_frame fails, retry without skip_frame also fails
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 69  # Codec error

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 69

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip]
        mock_exists.return_value = False

        # Glob call sequence for 2 attempts (skip_frame retry):
        # 1. img*.jpg after first attempt - empty
        # 2. *.jpg count after first attempt - empty
        # 3. *.jpg cleanup before retry - empty
        # 4. img*.jpg after retry - empty (retry failed)
        # 5. *.jpg count after retry - empty
        # All should return empty since both attempts fail
        def glob_side_effect(pattern):
            return []

        mock_glob.side_effect = glob_side_effect

        # Mock codec error detection
        mock_detect.return_value = True

        # Disable CPU threads
        mock_config.cpu_threads = 0

        # Should raise CodecNotSupportedError even when CPU threads disabled
        with pytest.raises(CodecNotSupportedError):
            generate_images("/test/video.mp4", temp_dir, "NVIDIA", None, mock_config)

        assert mock_detect.call_count >= 1
        assert mock_popen.call_count == 2  # Initial attempt + skip_frame retry, no CPU fallback (exception raised)

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_no_cpu_fallback_when_no_codec_error(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that CPU fallback doesn't trigger when error is not codec-related."""
        # Mock heuristic check - returns True to use skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        # First attempt with skip_frame fails, retry without skip_frame also fails
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 1  # Generic error

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 1

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip]
        mock_exists.return_value = False

        # Glob call sequence for 2 attempts (skip_frame retry):
        # 1. img*.jpg after first attempt - empty
        # 2. *.jpg count after first attempt - empty
        # 3. *.jpg cleanup before retry - empty
        # 4. img*.jpg after retry - empty (retry failed)
        # 5. *.jpg count after retry - empty
        # All should return empty since both attempts fail
        def glob_side_effect(pattern):
            return []

        mock_glob.side_effect = glob_side_effect

        # Mock codec error detection - no codec error detected
        mock_detect.return_value = False

        # Enable CPU threads
        mock_config.cpu_threads = 1

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video.mp4", temp_dir, "NVIDIA", None, mock_config
        )

        # Should fail (no fallback since not codec error)
        assert success is False
        assert image_count == 0
        assert mock_detect.call_count >= 1
        assert mock_popen.call_count == 2  # Initial attempt + skip_frame retry, no CPU fallback

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_gpu(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV RPU parsing errors should trigger a third DV-safe (fps+scale) FFmpeg run on GPU before CPU handoff."""
        # Heuristic check - returns 0 to allow skip_frame initially
        mock_run.return_value = MagicMock(returncode=0)

        # MediaInfo
        mock_info = MagicMock()
        # Force HDR filter chain initially (zscale/tonemap) so we can prove DV-safe retry removed it
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # FFmpeg processes: fail both attempts (skip + no-skip), then succeed with DV-safe filter
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 0

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        # Force output file reads to contain DV error
        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "Multiple Dolby Vision RPUs found in one AU. Skipping previous.\n"
        ]

        # No images produced until DV-safe retry, then one image exists (and then one renamed JPG exists)
        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"
        img_call_count = {"count": 0}

        def glob_side_effect(pattern):
            # Track only the img*.jpg pattern calls
            if "img*.jpg" in pattern:
                img_call_count["count"] += 1
                if img_call_count["count"] == 1:
                    return []  # after initial attempts
                if img_call_count["count"] == 2:
                    return [img1]  # after DV-safe attempt
                # rename stage: provide actual image filename
                return [img1]
            # Final recount after renaming
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        # Codec detection doesn't match; DV detector should still cause handoff
        mock_detect.return_value = False
        mock_config.cpu_threads = 1

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video_dv.mp4", temp_dir, "NVIDIA", None, mock_config
        )

        assert success is True
        assert image_count >= 1
        assert hw_used is True
        assert mock_popen.call_count == 3  # skip + no-skip + DV-safe retry

        # Assert the third invocation used the DV-safe vf: fps+scale only, no zscale/tonemap.
        # On NVIDIA the DV-safe retry keeps the GPU-scale win from issue #218 —
        # scale_cuda + hwdownload rather than a CPU scale on full-resolution frames.
        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "hwdownload" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dolby_vision_rpu_error_cpu_returns_failure(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV RPU parsing errors on CPU should fail cleanly without raising CodecNotSupportedError."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        # With DV-safe retry implemented, we expect a third attempt (DV-safe) even on CPU.
        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 187

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "Multiple Dolby Vision RPUs found in one AU. Skipping previous.\n"
        ]

        mock_glob.return_value = []
        mock_detect.return_value = False

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video_dv.mp4", temp_dir, None, None, mock_config
        )

        assert success is False
        assert image_count == 0
        assert hw_used is False
        assert mock_popen.call_count == 3

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dolby_vision_rpu_error_retries_with_dv_safe_filter_on_cpu(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV RPU parsing errors should trigger a third DV-safe (fps+scale) FFmpeg run on CPU."""
        mock_run.return_value = MagicMock(returncode=0)

        # Force HDR filter chain initially (zscale/tonemap) so we can prove DV-safe retry removed it
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # FFmpeg processes: fail both attempts (skip + no-skip), then succeed with DV-safe filter
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 0

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        # Force output file reads to contain DV error
        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "Multiple Dolby Vision RPUs found in one AU. Skipping previous.\n"
        ]

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"
        img_call_count = {"count": 0}

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                img_call_count["count"] += 1
                if img_call_count["count"] == 1:
                    return []
                if img_call_count["count"] == 2:
                    return [img1]
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        mock_detect.return_value = False

        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/video_dv.mp4", temp_dir, None, None, mock_config
        )

        assert success is True
        assert image_count >= 1
        assert hw_used is False
        assert mock_popen.call_count == 3

        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value


class TestMediaInfoImport:
    """Test MediaInfo import and validation."""

    def test_mediainfo_can_parse(self):
        """Test that MediaInfo is available and functional."""
        from pymediainfo import MediaInfo

        # This should not raise an exception in the test environment
        result = MediaInfo.can_parse()
        assert result is True or result is False  # Just check it doesn't crash


class TestDetailsMediaInfoCantRead:
    """A tag MediaInfo writes into its XML as it is (U+FFFF in a title) makes pymediainfo raise ``ParseError``; FFmpeg
    reads the file fine, so it gets its thumbnails without the details rather than failing."""

    def test_the_file_gets_its_thumbnails_and_one_warning_naming_it(
        self, video_mediainfo_cant_read, mock_config, tmp_path
    ):
        mock_config.ffmpeg_path = shutil.which("ffmpeg")
        mock_config.plex_bif_frame_interval = 1
        warnings: list[str] = []
        sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
        try:
            with failure_scope("mediainfo-cant-read"):
                success, image_count, *_ = generate_images(
                    video_mediainfo_cant_read, str(tmp_path / "frames"), None, None, mock_config
                )
        finally:
            logger.remove(sink)
        assert success and image_count >= 3
        assert len(os.listdir(tmp_path / "frames")) == image_count
        assert len(warnings) == 1 and video_mediainfo_cant_read in warnings[0], warnings


class TestDetailsMediaInfoCantReadKeepsHdr:
    """When MediaInfo's answer can't be read, ffprobe still tells an HDR file from an SDR one."""

    def test_hdr10_clip_is_still_seen_as_hdr(self, hdr10_video_mediainfo_cant_read):
        track = read_media_info(hdr10_video_mediainfo_cant_read).video_tracks[0]
        assert is_hdr_transfer(track.transfer_characteristics)

    def test_hdr10_clip_gets_the_tone_mapping_path(self, hdr10_video_mediainfo_cant_read, mock_config, tmp_path):
        mock_config.ffmpeg_path = shutil.which("ffmpeg")
        mock_config.plex_bif_frame_interval = 1
        infos: list[str] = []
        sink = logger.add(lambda m: infos.append(m.record["message"]), level="INFO")
        try:
            with failure_scope("mediainfo-cant-read-hdr"):
                success, *_ = generate_images(
                    hdr10_video_mediainfo_cant_read, str(tmp_path / "frames"), None, None, mock_config
                )
        finally:
            logger.remove(sink)
        assert success
        assert any("HDR transfer detected" in m for m in infos), infos

    @pytest.mark.parametrize(
        "side_data,expected",
        [
            pytest.param(
                [{"dv_profile": 5, "dv_bl_signal_compatibility_id": 0}], {"HDR_format": "Dolby Vision"}, id="dv-p5"
            ),
            pytest.param(
                [{"dv_profile": 8, "dv_bl_signal_compatibility_id": 1}],
                {"HDR_format": "Dolby Vision", "Transfer_characteristics": "PQ"},
                id="dv-p8-hdr10-base",
            ),
        ],
    )
    def test_side_data_is_mapped_to_mediainfo_fields(self, side_data, expected):
        stream = json.dumps({"streams": [{"color_transfer": "unknown", "side_data_list": side_data}]})
        done = subprocess.CompletedProcess([], 0, stdout=stream)
        with patch("media_preview_generator.processing.generator.subprocess.run", return_value=done):
            assert _video_details_from_ffprobe("x.mkv") == expected

    def test_a_file_ffprobe_cant_read_is_one_track_with_no_details(self):
        with patch("media_preview_generator.processing.generator.subprocess.run", side_effect=OSError):
            assert _video_details_from_ffprobe("x.mkv") == {}


class TestDVNoBackwardCompat:
    """Test Dolby Vision Profile 5 / no-backward-compat detection.

    Field pairs are real ``(hdr_format, transfer_characteristics)`` values
    from libmediainfo 24.12.  MediaInfo keeps the profile tag in
    ``HDR_Format_Profile``, so ``hdr_format`` alone cannot tell Profile 5
    from Profile 8.1; the declared transfer can.
    """

    @pytest.mark.parametrize(
        "hdr_format,transfer,expected",
        [
            # True Profile 5 (IPTPQc2): the stream declares no transfer.
            pytest.param("Dolby Vision", None, True, id="p5"),
            pytest.param("Dolby Vision / Dolby Vision", None, True, id="p5-listed-twice"),
            # Profile 8.1 web release without the ST 2086 SEI: an HDR10 base despite plain "Dolby Vision".
            pytest.param("Dolby Vision", "PQ", False, id="p8-no-st2086"),
            # A Profile 5 tag on an HDR10 base (Falling for Christmas) has these same inputs, since the profile tag
            # is not one; its file-level guard is ``p5_hdr10_base`` in test_dv_routing_real_fields.py.
            pytest.param("Dolby Vision / SMPTE ST 2086", "PQ", False, id="p8-st2086"),
            pytest.param("Dolby Vision / SMPTE ST 2094 App 4", "PQ", False, id="p8-hdr10plus"),
            # Profile 8.4: HLG base layer.
            pytest.param("Dolby Vision", "HLG", False, id="p84-hlg"),
            # Profile 7 and AV1 Profile 10 report the same hdr_format shape as Profile 8.1.
            pytest.param("Dolby Vision / SMPTE ST 2086 / SMPTE ST 2086", "PQ", False, id="p7-or-av1-p10"),
            # Standards' names for the transfer curves are accepted too.
            pytest.param("Dolby Vision", "SMPTE ST 2084", False, id="p8-transfer-standard-name"),
            # Profile 8.2/9 (SDR base) takes the Profile 5 path; no such file was seen in the library survey.
            pytest.param("Dolby Vision", "BT.709", True, id="p82-or-p9-sdr-base"),
            # Not Dolby Vision.
            pytest.param("SMPTE ST 2086", "PQ", False, id="hdr10"),
            pytest.param("SMPTE ST 2094 App 4", "PQ", False, id="hdr10plus"),
            pytest.param(None, "HLG", False, id="hlg-no-hdr-format"),
            pytest.param(None, "BT.709", False, id="sdr"),
            pytest.param(None, None, False, id="none"),
            pytest.param("None", None, False, id="none-string"),
            pytest.param("", None, False, id="empty"),
        ],
    )
    def test_detection(self, hdr_format: str | None, transfer: str | None, expected: bool) -> None:
        """_is_dv_no_backward_compat classifies each real MediaInfo field pair."""
        assert _is_dv_no_backward_compat(hdr_format, transfer) is expected


class TestIsDolbyVision:
    """Test _is_dolby_vision detection for all DV content."""

    @pytest.mark.parametrize(
        "hdr_format,expected",
        [
            # DV Profile 5 (no backward compat)
            ("Dolby Vision, Version 1.0, dvhe.05.06, BL+EL+RPU", True),
            # DV Profile 8 with HDR10 compat
            (
                "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2086",
                True,
            ),
            # DV Profile 7 with HDR10 compat
            ("Dolby Vision / HDR10 / SMPTE ST 2086", True),
            # DV with HLG compat
            ("Dolby Vision, HLG compatible", True),
            # DV + HDR10+
            (
                "Dolby Vision, Version 1.0, dvhe.08.06, BL+RPU, HDR10 compatible / SMPTE ST 2094 App 4",
                True,
            ),
            # Plain HDR10 (no DV) → False
            ("HDR10", False),
            ("SMPTE ST 2086, HDR10 compatible / SMPTE ST 2094 App 4", False),
            # Plain HLG → False
            ("HLG", False),
            # None / empty → False
            (None, False),
            ("None", False),
            ("", False),
        ],
    )
    def test_detection(self, hdr_format: str, expected: bool) -> None:
        """_is_dolby_vision correctly identifies any DV content."""
        assert _is_dolby_vision(hdr_format) is expected


class TestIsHdrTransfer:
    """is_hdr_transfer classifies MediaInfo's transfer_characteristics value."""

    @pytest.mark.parametrize(
        "transfer,expected",
        [
            ("PQ", True),
            ("HLG", True),
            # The standards' own names for the same two curves.
            ("SMPTE ST 2084", True),
            ("ARIB STD-B67", True),
            (" pq ", True),
            ("BT.709", False),
            ("BT.601", False),
            # BT.2020's own transfer is an SDR gamma curve, not HDR.
            ("BT.2020 (10-bit)", False),
            ("BT.2020 (12-bit)", False),
            ("Linear", False),
            (None, False),
            ("", False),
        ],
    )
    def test_detection(self, transfer: str | None, expected: bool) -> None:
        assert is_hdr_transfer(transfer) is expected


class TestDetectZscaleColorspaceError:
    """Test zscale colorspace error detection in stderr."""

    def test_detects_no_path_between_colorspaces(self) -> None:
        lines = [
            "[Parsed_zscale_1 @ 0x55eb] code 3074: no path between colorspaces",
            "Last message repeated 31 times",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_detects_generic_error_from_zscale(self) -> None:
        lines = [
            "[vf#0:0 @ 0x55eb] Error while filtering: Generic error in an external library",
            "zscale: Generic error in an external library",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_detects_bracketed_parsed_zscale_filter(self) -> None:
        """H1: FFmpeg logs [Parsed_zscale_1 @ 0x...] without bare 'zscale:' prefix."""
        lines = [
            "[Parsed_zscale_1 @ 0x55eb] Generic error in an external library",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_detects_bracketed_vf_zscale_filter(self) -> None:
        """H1: FFmpeg logs [vf#0:0/zscale @ 0x...] variant."""
        lines = [
            "[vf#0:0/zscale @ 0x5f3a928] Generic error in an external library",
        ]
        assert _detect_zscale_colorspace_error(lines) is True

    def test_no_match_on_unrelated_error(self) -> None:
        lines = ["Permission denied", "No such file or directory"]
        assert _detect_zscale_colorspace_error(lines) is False

    def test_empty_lines(self) -> None:
        assert _detect_zscale_colorspace_error([]) is False
        assert _detect_zscale_colorspace_error(None) is False


class TestProactiveDVSkip:
    """Integration test verifying DV routing: Profile 5 -> libplacebo, Profile 7/8 -> zscale."""

    def _run_generate(
        self,
        hdr_format_str,
        video_filename,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
        gpu=None,
        gpu_device_path=None,
        vulkan_device_info=None,
        transfer_characteristics=None,
    ):
        """Shared helper: run generate_images and return the FFmpeg args.

        ``transfer_characteristics`` defaults to ``None``, which is what a
        true Profile 5 stream declares; Profile 7/8 callers pass ``"PQ"``.

        ``vulkan_device_info`` patches ``get_vulkan_device_info`` for the
        duration of the call.  Defaults to a healthy hardware device so
        existing DV5 tests continue to hit the libplacebo path; tests
        for the software-fallback guard pass a software dict.

        ``gpu_device_path`` is forwarded to ``generate_images`` so tests
        can select between the VAAPI+Vulkan interop DV5 path (pass a
        ``/dev/dri/renderD*`` path) and the software-fallback DV5 path
        (pass ``None``).
        """
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [
            MagicMock(hdr_format=hdr_format_str, transfer_characteristics=transfer_characteristics)
        ]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        # Accept either a dict (legacy test shape) or a VulkanProbeResult,
        # normalising to VulkanProbeResult for the production contract.
        if vulkan_device_info is None:
            default_vulkan = VulkanProbeResult(device="Quadro P4000 (NVIDIA)", is_software=False)
        elif isinstance(vulkan_device_info, VulkanProbeResult):
            default_vulkan = vulkan_device_info
        else:
            default_vulkan = VulkanProbeResult(
                device=vulkan_device_info.get("device"),
                is_software=vulkan_device_info.get("is_software", False),
            )

        with patch(
            "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
            return_value=default_vulkan,
        ):
            success, image_count, hw_used, seconds, speed, *_ = generate_images(
                video_filename, temp_dir, gpu, gpu_device_path, mock_config
            )

        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 1

        return mock_popen.call_args_list[0][0][0]

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_uses_libplacebo(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 on an Intel host with a VAAPI render device
        uses VAAPI hardware decode + OpenCL tonemap_opencl via
        jellyfin-ffmpeg's DV-aware patch.

        Intel's VAAPI→Vulkan libplacebo path is broken upstream on
        Mesa ANV (libplacebo vkCreateImage returns VK_ERROR_OUT_OF_
        DEVICE_MEMORY).  Jellyfin-ffmpeg's tonemap_opencl reads DV RPU
        side-data correctly and produces correct colours at ~17x on
        Intel UHD 770.  See issue #212.
        """
        args = self._run_generate(
            "Dolby Vision",
            "/test/dv_profile5.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="INTEL",
            gpu_device_path="/dev/dri/renderD128",
        )

        # Intel DV5 path initialises VAAPI on the render node and an
        # OpenCL context derived from that VAAPI device (ocl@va).
        init_indices = [i for i, a in enumerate(args) if a == "-init_hw_device"]
        assert len(init_indices) == 2, "Intel DV5 path must initialise VAAPI + derived OpenCL (2 devices)"
        assert args[init_indices[0] + 1] == "vaapi=va:/dev/dri/renderD128"
        assert args[init_indices[1] + 1] == "opencl=ocl@va"
        assert "-filter_hw_device" in args
        assert args[args.index("-filter_hw_device") + 1] == "ocl"

        # -hwaccel must be vaapi, output format kept on-device (vaapi),
        # device reference must match the named "va" VAAPI context.
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "vaapi"
        assert "-hwaccel_device" in args
        assert args[args.index("-hwaccel_device") + 1] == "va"
        assert "-hwaccel_output_format" in args
        assert args[args.index("-hwaccel_output_format") + 1] == "vaapi"

        # Hardware-decoded DV5, so -threads:v 1.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

        # Skip-frame stays disabled on DV5 because RPU side data has
        # inter-frame dependencies.
        assert "-skip_frame:v" not in args

        # Filter chain: fps first (drop frames BEFORE the expensive tonemap
        # step), then setparams to tag the base layer as BT.2020/PQ, then
        # hwmap to OpenCL and tonemap_opencl, then hwdownload for mjpeg.
        vf = args[args.index("-vf") + 1]
        assert "hwmap=derive_device=opencl:mode=read" in vf
        assert "tonemap_opencl=" in vf
        assert "hwmap=derive_device=vulkan" not in vf, (
            "Intel DV5 uses OpenCL, not Vulkan (libplacebo interop is broken)"
        )
        assert "libplacebo=" not in vf, "Intel DV5 path uses tonemap_opencl, not libplacebo"
        assert "hwupload" not in vf
        assert "hwdownload" in vf
        assert "zscale" not in vf
        # fps filter must come BEFORE hwmap — that's the 6x speed-up
        # (benchmarked 2.71x→17.3x on UHD 770).
        fps_pos = vf.find("fps=fps=")
        hwmap_pos = vf.find("hwmap=")
        assert 0 <= fps_pos < hwmap_pos, (
            "fps filter must run BEFORE hwmap so we drop frames at the VAAPI surface and don't waste tonemap cycles"
        )

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_amd_uses_vaapi_hwaccel(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """AMD on DV Profile 5 uses VAAPI decode + Vulkan libplacebo
        via the Jellyfin ``drm=dr → vaapi=va@dr → vulkan=vk@dr`` pattern.
        Intel takes a separate OpenCL tonemap path (see Intel test above)
        because Mesa ANV's Vulkan DMA-BUF import is broken for DV5 format
        modifiers.  AMD's Mesa RADV has working DMA-BUF interop so we
        keep the libplacebo path there.
        """
        args = self._run_generate(
            "Dolby Vision",
            "/test/dv_profile5.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="AMD",
            gpu_device_path="/dev/dri/renderD129",
        )

        # Same VAAPI + vk@va + hwmap structure as Intel, different node.
        init_indices = [i for i, a in enumerate(args) if a == "-init_hw_device"]
        assert len(init_indices) == 3
        assert args[init_indices[0] + 1] == "drm=dr:/dev/dri/renderD129"
        assert args[init_indices[1] + 1] == "vaapi=va@dr"
        assert args[init_indices[2] + 1] == "vulkan=vk@dr"
        assert "-hwaccel" in args and args[args.index("-hwaccel") + 1] == "vaapi"
        assert args[args.index("-hwaccel_output_format") + 1] == "vaapi"
        assert args[args.index("-threads:v") + 1] == "1"
        vf = args[args.index("-vf") + 1]
        assert "hwmap=derive_device=vulkan" in vf
        assert "libplacebo" in vf
        assert "hwupload" not in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_non_dri_falls_back_to_sw_decode(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 on a Linux GPU without a /dev/dri render node
        (defensive edge case) falls back to software decode + the
        plain ``vulkan=vk`` libplacebo path with ``-threads:v 0`` so
        the HEVC decoder can still saturate CPU cores.
        """
        args = self._run_generate(
            "Dolby Vision",
            "/test/dv_profile5.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="INTEL",
            gpu_device_path=None,
        )

        # No VAAPI hwaccel, vanilla vulkan=vk device init only.
        assert "-hwaccel" not in args
        init_indices = [i for i, a in enumerate(args) if a == "-init_hw_device"]
        assert len(init_indices) == 1
        assert args[init_indices[0] + 1] == "vulkan=vk"

        # Software decode path must uncap the video decoder threads.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "0"

        # Filter chain falls back to hwupload (CPU frame → Vulkan).
        vf = args[args.index("-vf") + 1]
        assert "hwupload" in vf
        assert "hwmap=derive_device=vulkan" not in vf
        assert "libplacebo" in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_software_vulkan_uses_dv_safe_filter(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV5 + software Vulkan drops libplacebo and uses the DV-safe filter.

        Regression guard for issue #213's latent bug: if Vulkan falls
        back to ``llvmpipe`` (e.g. because the container's NVIDIA GLVND
        config is missing), libplacebo would silently produce green
        thumbnails.  The pre-flight guard in ``generate_images`` checks
        ``get_vulkan_device_info()["is_software"]`` and, when true,
        skips libplacebo entirely and falls through to a plain
        ``fps=...,scale=...`` filter chain with no tone mapping, whose
        thumbnails get a green and purple tint.
        """
        args = self._run_generate(
            "Dolby Vision",
            "/test/dv_profile5_sw_vulkan.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="NVIDIA",
            vulkan_device_info={
                "device": "llvmpipe (LLVM 18.1.3, 256 bits) (software)",
                "is_software": True,
            },
        )

        # The pre-flight guard must bypass libplacebo entirely — no
        # Vulkan init, no hwupload, no libplacebo filter.
        assert "-init_hw_device" not in args, "Software Vulkan must not initialise a Vulkan device for DV5"
        assert "-filter_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" not in vf, "Software Vulkan must not run libplacebo on DV5 (green overlay bug)"
        assert "hwupload" not in vf
        # Zscale is also wrong on DV5 (no HDR10 base layer) — the
        # fallback must use the plain fps+scale chain, same as the
        # DV-safe retry target.  On NVIDIA, fps is followed by
        # scale_cuda+hwdownload (issue #218 GPU-scale optimisation)
        # rather than a CPU scale.
        assert "zscale" not in vf
        assert "tonemap" not in vf
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        assert "hwdownload" in vf
        assert vf.startswith("fps=fps=")

    @pytest.mark.parametrize(
        ("node", "caps", "says", "doesnt_say"),
        [
            pytest.param(True, None, "set NVIDIA_DRIVER_CAPABILITIES=all", "--runtime=nvidia", id="node-caps-unset"),
            pytest.param(
                True,
                "all",
                "start the container with --runtime=nvidia",
                "set NVIDIA_DRIVER_CAPABILITIES",
                id="node-caps-all",
            ),
            pytest.param(
                False, "all", "See the dashboard notification centre", "NVIDIA_DRIVER_CAPABILITIES", id="app-caps-all"
            ),
        ],
    )
    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_a_dv5_file_without_vulkan_names_the_fix_its_container_still_needs(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
        monkeypatch,
        node,
        caps,
        says,
        doesnt_say,
    ):
        """On a node the file's own line is the only place the fix is said; it mustn't ask for a setting already made."""
        from loguru import logger

        if caps is None:
            monkeypatch.delenv("NVIDIA_DRIVER_CAPABILITIES", raising=False)
        else:
            monkeypatch.setenv("NVIDIA_DRIVER_CAPABILITIES", caps)
        monkeypatch.setattr("media_preview_generator.processing.generator.is_node_mode", lambda: node)
        messages: list[str] = []
        sink_id = logger.add(lambda msg: messages.append(str(msg)), level="WARNING", format="{message}")
        try:
            self._run_generate(
                "Dolby Vision",
                "/test/dv_profile5_sw_vulkan.mkv",
                mock_detect,
                mock_glob,
                mock_sleep,
                mock_file,
                mock_exists,
                mock_remove,
                mock_rename,
                mock_run,
                mock_popen,
                mock_mediainfo,
                temp_dir,
                mock_config,
                gpu="NVIDIA",
                vulkan_device_info={"device": "llvmpipe (LLVM 18.1.3, 256 bits) (software)", "is_software": True},
            )
        finally:
            logger.remove(sink_id)

        hits = [m for m in messages if "needs a real GPU with Vulkan" in m]
        assert len(hits) == 1, messages
        assert says in hits[0]
        assert doesnt_say not in hits[0]

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_no_vulkan_device_uses_dv_safe_filter(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV5 + no Vulkan device at all also falls back to DV-safe filter.

        Same guard as the software-Vulkan case but with ``device=None``
        instead of a software rasteriser string.  Both mean "libplacebo
        is not usable" and must route to the fps+scale chain.
        """
        args = self._run_generate(
            "Dolby Vision",
            "/test/dv_profile5_no_vulkan.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="INTEL",
            vulkan_device_info={"device": None, "is_software": False},
        )

        assert "-init_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" not in vf
        assert "hwupload" not in vf
        assert "zscale" not in vf
        assert vf.startswith("fps=fps=")

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_nvidia_uses_nvdec(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 on NVIDIA hosts decodes via NVDEC before libplacebo.

        Benchmarked 2026-04 against ``linuxserver/ffmpeg:8.0.1-cli-ls56``:
        5 minutes of 4K HEVC DV Profile 5 content runs at ~12x on NVDEC vs
        ~4x on software decode — a ~3x end-to-end speedup — with
        visually identical output after the libplacebo ``apply_dolbyvision``
        tone map.  Any code change that silently drops ``-hwaccel cuda``
        from this path would regress the speedup.
        """
        args = self._run_generate(
            "Dolby Vision",
            "/test/dv_profile5_nvidia.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="NVIDIA",
        )

        # libplacebo path still used for tone mapping
        assert "-init_hw_device" in args
        assert args[args.index("-init_hw_device") + 1] == "vulkan=vk"
        assert "-filter_hw_device" in args
        assert args[args.index("-filter_hw_device") + 1] == "vk"

        # NVDEC is attached BEFORE the input file
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert args.index("-hwaccel") < args.index("-i")

        # HW decode is active, so the video-thread cap reapplies to
        # prevent decoder thread oversubscription across GPU workers.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

        # RPU side-data still has inter-frame dependencies even under
        # NVDEC — keyframe-only decode must stay off.
        assert "-skip_frame:v" not in args

        # Filter graph is unchanged: libplacebo does the DV tone map.
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" in vf
        assert "hwupload" in vf
        assert "zscale" not in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_cpu_skips_hwaccel(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 5 with no GPU configured stays on software decode."""
        args = self._run_generate(
            "Dolby Vision",
            "/test/dv_profile5_cpu.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu=None,
        )

        assert "-init_hw_device" in args
        assert "-hwaccel" not in args
        assert "-threads:v" not in args
        assert "-skip_frame:v" not in args
        vf = args[args.index("-vf") + 1]
        assert "libplacebo" in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile8_hdr10_uses_zscale(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 8 with HDR10 fallback uses zscale/tonemap on the HDR10 base layer."""
        args = self._run_generate(
            "Dolby Vision / SMPTE ST 2086",
            "/test/dv_profile8_hdr10.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            transfer_characteristics="PQ",
        )

        # Profile 7/8 uses the HDR10 base layer — no libplacebo needed
        assert "-init_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "zscale" in vf
        assert "tonemap" in vf
        assert "libplacebo" not in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_hdr10plus_uses_zscale(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV + HDR10+ (the Severance scenario from #178) uses zscale via HDR10 base layer."""
        args = self._run_generate(
            "Dolby Vision / SMPTE ST 2094 App 4",
            "/test/dv_hdr10plus.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            transfer_characteristics="PQ",
        )

        assert "-init_hw_device" not in args
        vf = args[args.index("-vf") + 1]
        assert "zscale" in vf
        assert "tonemap" in vf
        assert "libplacebo" not in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_dv_profile8_with_gpu_uses_cuda_and_zscale(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """DV Profile 8 on GPU uses CUDA hwaccel + zscale (reads HDR10 base layer)."""
        args = self._run_generate(
            "Dolby Vision / SMPTE ST 2086",
            "/test/dv_gpu.mkv",
            mock_detect,
            mock_glob,
            mock_sleep,
            mock_file,
            mock_exists,
            mock_remove,
            mock_rename,
            mock_run,
            mock_popen,
            mock_mediainfo,
            temp_dir,
            mock_config,
            gpu="NVIDIA",
            transfer_characteristics="PQ",
        )

        # Profile 7/8 with HDR10 fallback — standard path with GPU decode
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-init_hw_device" not in args

        # HW decode is active so the video-thread cap must still be present
        # (prevents decoder thread oversubscription across GPU workers).
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

        vf = args[args.index("-vf") + 1]
        assert "zscale" in vf
        assert "tonemap" in vf
        assert "libplacebo" not in vf


class TestLibplaceboFallback:
    """Test that libplacebo failure triggers the DV-safe retry with basic fps+scale."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_profile5_libplacebo_failure_falls_back(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """When libplacebo FFmpeg call fails, retry with basic fps+scale chain."""
        # Arrange — heuristic probe fails (no skip)
        mock_run.return_value = MagicMock(returncode=1, stderr="")

        # MediaInfo: DV Profile 5
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="Dolby Vision", transfer_characteristics=None)]
        mock_mediainfo.parse.return_value = mock_info

        # First FFmpeg call (libplacebo) fails, second (DV-safe) succeeds
        mock_proc_fail = MagicMock()
        mock_proc_fail.poll.side_effect = [None, 0]
        mock_proc_fail.returncode = 1

        mock_proc_ok = MagicMock()
        mock_proc_ok.poll.side_effect = [None, 0]
        mock_proc_ok.returncode = 0

        mock_popen.side_effect = [mock_proc_fail, mock_proc_ok]

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        call_count = [0]

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                call_count[0] += 1
                # First img count (after libplacebo fail) returns 0,
                # second (after DV-safe retry) returns 1
                if call_count[0] <= 1:
                    return []
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        # Act — force a healthy Vulkan device so the pre-flight software
        # guard added in Change 3 lets the libplacebo path build normally.
        # This test specifically exercises the in-flight libplacebo failure
        # path, not the software-Vulkan pre-flight fallback.
        with patch(
            "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
            return_value=VulkanProbeResult(device="Quadro P4000", is_software=False),
        ):
            success, image_count, hw_used, seconds, speed, *_ = generate_images(
                "/test/dv_profile5.mkv", temp_dir, None, None, mock_config
            )

        # Assert — success after DV-safe retry
        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 2

        # First call should have libplacebo + vulkan init
        first_args = mock_popen.call_args_list[0][0][0]
        assert "-init_hw_device" in first_args
        assert first_args[first_args.index("-init_hw_device") + 1] == "vulkan=vk"
        assert "-filter_hw_device" in first_args
        vf1_idx = first_args.index("-vf")
        assert "libplacebo" in first_args[vf1_idx + 1]

        # Second call (DV-safe retry) should be basic fps+scale, no vulkan
        second_args = mock_popen.call_args_list[1][0][0]
        assert "-init_hw_device" not in second_args
        assert "-filter_hw_device" not in second_args
        vf2_idx = second_args.index("-vf")
        vf2_value = second_args[vf2_idx + 1]
        assert "libplacebo" not in vf2_value
        assert "zscale" not in vf2_value
        assert "fps=" in vf2_value
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf2_value


class TestZscaleErrorRetry:
    """M6: Test that zscale colorspace errors trigger the DV-safe retry path."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_zscale_error_triggers_dv_safe_retry(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Bracketed zscale error in stderr triggers DV-safe retry (fps+scale)."""
        # Arrange — heuristic probe allows skip
        mock_run.return_value = MagicMock(returncode=0)

        # MediaInfo: plain HDR10 (not DV) so initial chain includes zscale/tonemap
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # FFmpeg: fail with rc=187 twice (skip + no-skip), succeed on DV-safe retry
        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 187

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 187

        mock_proc_dv_safe = MagicMock()
        mock_proc_dv_safe.poll.side_effect = [None, 0]
        mock_proc_dv_safe.returncode = 0

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip, mock_proc_dv_safe]

        mock_exists.return_value = True
        # Stderr containing the bracketed zscale error pattern (H1)
        mock_file.return_value.readlines.return_value = [
            "[Parsed_zscale_1 @ 0x55eb] Generic error in an external library\n"
        ]

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"
        img_call_count = {"count": 0}

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                img_call_count["count"] += 1
                if img_call_count["count"] == 1:
                    return []  # initial count: no images
                if img_call_count["count"] == 2:
                    return [img1]  # after DV-safe retry
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect
        mock_detect.return_value = False
        mock_config.cpu_threads = 1

        # Act
        success, image_count, hw_used, seconds, speed, *_ = generate_images(
            "/test/hdr10_zscale_crash.mkv", temp_dir, "NVIDIA", None, mock_config
        )

        # Assert — success after 3 Popen calls, third uses DV-safe vf
        assert success is True
        assert image_count >= 1
        assert mock_popen.call_count == 3

        third_args = mock_popen.call_args_list[2][0][0]
        vf_index = third_args.index("-vf")
        vf_value = third_args[vf_index + 1]
        # NVIDIA DV-safe retry keeps scale_cuda + hwdownload (issue #218).
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value
        assert "hwdownload" in vf_value
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value


class TestDVSafeRetryGpuFailure:
    """M8: DV-safe retry fails on GPU — should raise CodecNotSupportedError."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_generate_images_dv_safe_retry_gpu_failure_raises_codec_error(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """All FFmpeg attempts fail with zscale error on GPU → CodecNotSupportedError."""
        # Arrange — heuristic allows skip
        mock_run.return_value = MagicMock(returncode=0)

        # MediaInfo: plain HDR10 → initial chain has zscale/tonemap
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="HDR10")]
        mock_mediainfo.parse.return_value = mock_info

        # All three FFmpeg calls fail
        for _ in range(3):
            proc = MagicMock()
            proc.poll.side_effect = [None, 0]
            proc.returncode = 187
        mock_popen.side_effect = [
            MagicMock(poll=MagicMock(side_effect=[None, 0]), returncode=187),
            MagicMock(poll=MagicMock(side_effect=[None, 0]), returncode=187),
            MagicMock(poll=MagicMock(side_effect=[None, 0]), returncode=187),
        ]

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = [
            "[Parsed_zscale_1 @ 0x55eb] Generic error in an external library\n"
        ]

        # No images at any stage
        mock_glob.return_value = []
        mock_detect.return_value = False
        mock_config.cpu_threads = 1

        # Act & Assert — GPU path raises CodecNotSupportedError
        with pytest.raises(CodecNotSupportedError):
            generate_images("/test/always_fails.mkv", temp_dir, "NVIDIA", None, mock_config)

        assert mock_popen.call_count == 3


class TestDynamicNpl:
    """M7: Test that npl is always 100 (SDR reference white) regardless of MaxCLL."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_npl_always_100_with_maxcll(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """npl is always 100 (SDR reference) even when MaxCLL is available."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        video_track = MagicMock()
        video_track.hdr_format = "HDR10"
        video_track.maximum_content_light_level = "1000"
        mock_info.video_tracks = [video_track]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/hdr10_1000nit.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "npl=100" in vf_value
        assert "npl=1000" not in vf_value

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_npl_always_100_without_maxcll(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """npl is always 100 (SDR reference) even when MaxCLL is absent."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        video_track = MagicMock()
        video_track.hdr_format = "HDR10"
        video_track.maximum_content_light_level = None
        mock_info.video_tracks = [video_track]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/hdr10_no_maxcll.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "npl=100" in vf_value

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_hdr_uses_desat_0(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """HDR filter chain uses desat=0 to preserve colour saturation."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        video_track = MagicMock()
        video_track.hdr_format = "HDR10"
        video_track.maximum_content_light_level = "N/A"
        mock_info.video_tracks = [video_track]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/hdr10_bad_maxcll.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "desat=0" in vf_value
        assert "npl=100" in vf_value


class TestDetectDolbyVisionRPUError:
    """L11: Unit tests for _detect_dolby_vision_rpu_error."""

    def test_detects_multiple_rpus(self) -> None:
        lines = ["Multiple Dolby Vision RPUs found in one AU"]
        assert _detect_dolby_vision_rpu_error(lines) is True

    def test_detects_multiple_rpus_with_skipping(self) -> None:
        lines = ["multiple dolby vision rpus found in one au. skipping previous."]
        assert _detect_dolby_vision_rpu_error(lines) is True

    def test_case_insensitive(self) -> None:
        lines = ["MULTIPLE DOLBY VISION RPUS FOUND IN ONE AU"]
        assert _detect_dolby_vision_rpu_error(lines) is True

    def test_no_match_on_unrelated(self) -> None:
        lines = ["Codec error: unsupported codec"]
        assert _detect_dolby_vision_rpu_error(lines) is False

    def test_empty_input(self) -> None:
        assert _detect_dolby_vision_rpu_error([]) is False
        assert _detect_dolby_vision_rpu_error(None) is False


class TestSaveFFmpegFailureLog:
    """L11: Unit tests for _save_ffmpeg_failure_log."""

    def test_creates_log_file(self, tmp_path, monkeypatch) -> None:
        """Log file is created with expected content."""
        log_dir = str(tmp_path)
        monkeypatch.setenv("CONFIG_DIR", log_dir)

        _save_ffmpeg_failure_log("/media/test.mkv", 187, ["error line 1", "error line 2"])

        ffmpeg_log_dir = tmp_path / "logs" / "ffmpeg_failures"
        log_files = list(ffmpeg_log_dir.glob("*.log"))
        assert len(log_files) == 1

        content = log_files[0].read_text(encoding="utf-8")
        assert "file: /media/test.mkv" in content
        assert "exit_code: 187" in content
        assert "signal_killed: False" in content
        assert "exit_diagnosis: high_exit_non_signal" in content
        assert "error line 1" in content
        assert "error line 2" in content

    def test_caps_at_500_files(self, tmp_path, monkeypatch) -> None:
        """Directory is capped at 500 log files."""
        log_dir = str(tmp_path)
        monkeypatch.setenv("CONFIG_DIR", log_dir)

        ffmpeg_log_dir = tmp_path / "logs" / "ffmpeg_failures"
        ffmpeg_log_dir.mkdir(parents=True)

        # Pre-create 500 files
        import time as _time

        for i in range(500):
            (ffmpeg_log_dir / f"old_{i:04d}.log").write_text("old")
            _time.sleep(0.001)  # ensure distinct mtimes

        _save_ffmpeg_failure_log("/media/new.mkv", 1, ["new error"])

        log_files = list(ffmpeg_log_dir.glob("*.log"))
        assert len(log_files) <= 501  # 500 cap + new file (oldest removed)

    def test_handles_oserror_gracefully(self, monkeypatch, tmp_path) -> None:
        """OSError during directory creation is swallowed."""
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, so no directory can be created beneath it")
        monkeypatch.setenv("CONFIG_DIR", str(blocker / "config"))

        _save_ffmpeg_failure_log("/media/test.mkv", 1, ["error"])

        assert blocker.read_text() == "a file, so no directory can be created beneath it"
        assert not (blocker / "config").exists()


class TestDiagnoseFFmpegExitCode:
    """Unit tests for _diagnose_ffmpeg_exit_code."""

    @pytest.mark.parametrize(
        ("returncode", "expected"),
        [
            (0, "success"),
            (129, "signal:SIGHUP"),
            (130, "signal:SIGINT"),
            (137, "signal:SIGKILL"),
            (143, "signal:SIGTERM"),
            (251, "io_error"),
            (187, "high_exit_non_signal"),
            (-15, "signal:15"),
            (1, "error"),
        ],
    )
    def test_classifies_exit_codes(self, returncode: int, expected: str) -> None:
        assert _diagnose_ffmpeg_exit_code(returncode) == expected


class TestVerifyTmpFolderHealth:
    """Unit tests for _verify_tmp_folder_health."""

    def test_returns_healthy_for_writable_directory(self, tmp_path) -> None:
        ok, messages = _verify_tmp_folder_health(str(tmp_path))
        assert ok is True
        assert messages == []

    def test_returns_error_for_unwritable_directory(self, tmp_path) -> None:
        with patch("builtins.open", side_effect=OSError("read-only")):
            ok, messages = _verify_tmp_folder_health(str(tmp_path))
        assert ok is False
        assert messages
        assert "not writable" in messages[0].lower()

    def test_warns_when_disk_space_low(self, tmp_path) -> None:
        with patch("media_preview_generator.processing.generator.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = MagicMock(free=0)
            ok, messages = _verify_tmp_folder_health(str(tmp_path), min_free_mb=1)
        assert ok is True
        assert messages
        assert "low free space" in messages[0].lower()


class TestHdrFormatNoneString:
    """L12: hdr_format='None' string should produce the SDR filter path."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_hdr_format_none_string_uses_sdr_path(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """When MediaInfo returns 'None' (string), SDR path is taken (no zscale/tonemap)."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="None")]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        success, *_ = generate_images("/test/sdr_none_string.mkv", temp_dir, None, None, mock_config)

        assert success is True
        first_args = mock_popen.call_args_list[0][0][0]
        vf_index = first_args.index("-vf")
        vf_value = first_args[vf_index + 1]
        assert "zscale" not in vf_value
        assert "tonemap" not in vf_value
        assert "fps=" in vf_value
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf_value


class TestHdrVersusSdrFilterChain:
    """Each MediaInfo shape that decides HDR vs SDR must pick the right filter chain.

    MediaInfo only fills ``HDR_Format`` when the file carries HDR metadata
    (mastering display, HDR10+, Dolby Vision).  A PQ or HLG stream with no
    such metadata still needs tone mapping, or its thumbnails come out
    washed out.  BT.2020 primaries alone do not make a file HDR.

    The GPU variants of each chain are keyed only on the chosen path, which
    ``TestGpuScaleOptimisation`` already covers, so this matrix runs on CPU.
    """

    FPS = "fps=fps=0.2:round=up"
    SCALE = "scale=w=320:h=240:force_original_aspect_ratio=decrease"
    TONEMAP = (
        "zscale=t=linear:npl=100,format=gbrpf32le,"
        "zscale=p=bt709,tonemap=hable:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
    )
    SDR_VF = f"{FPS},{SCALE}"
    HDR_VF = f"{FPS},{TONEMAP},{SCALE}"
    DV5_VF = build_dv5_vf(path_kind=DV5_PATH_LIBPLACEBO, tonemap_algorithm="hable", fps_value=0.2, base_scale=SCALE)

    @staticmethod
    def _first_vf(mock_config, *, hdr_format, transfer, primaries) -> str:
        track = MagicMock(hdr_format=hdr_format, transfer_characteristics=transfer, color_primaries=primaries)
        info = MagicMock()
        info.video_tracks = [track]
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0
        with (
            patch("media_preview_generator.processing.generator.MediaInfo") as mock_mediainfo,
            patch("subprocess.Popen", return_value=proc) as mock_popen,
            patch("os.path.exists", return_value=False),
            patch("builtins.open", new_callable=mock_open),
            patch("time.sleep"),
            patch("media_preview_generator.processing.generator.glob.glob", return_value=[]),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch(
                "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
                return_value=VulkanProbeResult(device="Quadro P4000 (NVIDIA)", is_software=False),
            ),
        ):
            mock_mediainfo.parse.return_value = info
            generate_images("/test/v.mkv", "/tmp/o", None, None, mock_config)
        first_args = mock_popen.call_args_list[0][0][0]
        return first_args[first_args.index("-vf") + 1]

    @pytest.mark.parametrize(
        "hdr_format,transfer,primaries,expected",
        [
            pytest.param("SMPTE ST 2086, HDR10 compatible", "PQ", "BT.2020", "HDR", id="hdr10"),
            pytest.param(
                "SMPTE ST 2094 App 4, Version 1, HDR10+ Profile B compatible", "PQ", "BT.2020", "HDR", id="hdr10plus"
            ),
            # A Profile 5 tag on an HDR10 base (Falling for Christmas) has these same inputs, since the profile tag
            # is not one; its file-level guard is ``p5_hdr10_base`` in test_dv_routing_real_fields.py.
            pytest.param("Dolby Vision / SMPTE ST 2086", "PQ", "BT.2020", "HDR", id="dv-profile8"),
            # Profile 8.1 web release without the ST 2086 SEI (The Lion King, DSNP): plain "Dolby Vision", PQ base.
            pytest.param("Dolby Vision", "PQ", "BT.2020", "HDR", id="dv-profile8-no-st2086"),
            pytest.param("Dolby Vision", "HLG", "BT.2020", "HDR", id="dv-profile84-hlg"),
            # True Profile 5 (IPTPQc2) declares no transfer or primaries.
            pytest.param("Dolby Vision", None, None, "DV5", id="dv-profile5"),
            # Lab file: Cosmos Laundromat HDR (Netflix Open Content) — PQ, P3 primaries, no HDR metadata.
            pytest.param(None, "PQ", "Display P3", "HDR", id="pq-no-hdr-format-p3"),
            pytest.param(None, "PQ", "BT.2020", "HDR", id="pq-no-hdr-format"),
            pytest.param("None", "PQ", "BT.2020", "HDR", id="pq-hdr-format-none-string"),
            pytest.param(None, "HLG", "BT.2020", "HDR", id="hlg-no-hdr-format"),
            pytest.param(None, "BT.709", "BT.709", "SDR", id="sdr-bt709"),
            pytest.param(None, "BT.709", "BT.2020", "SDR", id="sdr-bt2020-primaries-bt709-transfer"),
            pytest.param(None, "BT.2020 (10-bit)", "BT.2020", "SDR", id="sdr-bt2020-primaries-bt2020-transfer"),
            pytest.param(None, None, None, "SDR", id="no-colour-metadata"),
        ],
    )
    def test_picks_filter_chain_when_track_metadata_is(
        self, hdr_format, transfer, primaries, expected, mock_config
    ) -> None:
        expected_vf = {"HDR": self.HDR_VF, "SDR": self.SDR_VF, "DV5": self.DV5_VF}[expected]

        vf = self._first_vf(mock_config, hdr_format=hdr_format, transfer=transfer, primaries=primaries)

        assert vf == expected_vf


class TestGpuScaleOptimisation:
    """Issue #218: keep decoded frames on the GPU through downscale so
    per-worker RSS drops from ~1 GB to ~300 MB on 4K HDR10.
    """

    @staticmethod
    def _run(mock_mediainfo, mock_popen, mock_config, *, gpu, gpu_device, hdr_fmt, transfer=None):
        info = MagicMock()
        info.video_tracks = [MagicMock(hdr_format=hdr_fmt, transfer_characteristics=transfer)]
        mock_mediainfo.parse.return_value = info
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0
        mock_popen.return_value = proc
        with (
            patch("os.path.exists", return_value=False),
            patch("builtins.open", new_callable=mock_open),
            patch("time.sleep"),
            patch("media_preview_generator.processing.generator.glob.glob", return_value=[]),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
        ):
            generate_images("/test/v.mp4", "/tmp/o", gpu, gpu_device, mock_config)
        return mock_popen.call_args[0][0]

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    def test_nvidia_sdr_uses_scale_cuda_with_hwaccel_output_format(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="NVIDIA",
            gpu_device="cuda",
            hdr_fmt=None,
        )
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-hwaccel_output_format" in args
        assert args[args.index("-hwaccel_output_format") + 1] == "cuda"
        vf = args[args.index("-vf") + 1]
        assert "scale_cuda=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        assert "force_divisible_by=2" in vf, "Letterboxed 2.4:1 content needs even output dims or zscale rejects it"
        assert "format=nv12" in vf
        assert "hwdownload" in vf
        # The old CPU-scale bug must not re-appear.
        assert "scale=w=320" not in vf.replace("scale_cuda=w=320", "")
        # Decoder thread cap unchanged.
        assert "-threads:v" in args
        assert args[args.index("-threads:v") + 1] == "1"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    def test_nvidia_hdr10_downscales_on_gpu_before_zscale(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="NVIDIA",
            gpu_device="cuda",
            hdr_fmt="SMPTE ST 2086",
        )
        vf = args[args.index("-vf") + 1]
        # scale_cuda must appear BEFORE zscale so zscale tonemaps a
        # 320x240 frame, not a 4K one.
        scale_idx = vf.find("scale_cuda=")
        zscale_idx = vf.find("zscale=t=linear")
        assert scale_idx != -1 and zscale_idx != -1
        assert scale_idx < zscale_idx
        assert "format=p010le" in vf, "HDR10 path must keep 10-bit through GPU scale"
        assert "tonemap=hable" in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    def test_vaapi_sdr_uses_scale_vaapi_with_even_parity_safety(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="INTEL",
            gpu_device="/dev/dri/renderD128",
            hdr_fmt=None,
        )
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "vaapi"
        # Modern -hwaccel_device pairs with -hwaccel_output_format;
        # the deprecated -vaapi_device would break this pairing.
        assert "-hwaccel_device" in args
        assert args[args.index("-hwaccel_device") + 1] == "/dev/dri/renderD128"
        assert "-vaapi_device" not in args
        assert "-hwaccel_output_format" in args
        assert args[args.index("-hwaccel_output_format") + 1] == "vaapi"
        vf = args[args.index("-vf") + 1]
        assert "scale_vaapi=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        assert "hwdownload" in vf
        # scale_vaapi lacks force_divisible_by; the CPU parity fix after
        # hwdownload snaps letterboxed odd heights (e.g. 320x133) to even.
        assert "scale=trunc(iw/2)*2:trunc(ih/2)*2" in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    def test_vaapi_hdr10_downscales_on_gpu_before_zscale(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="AMD",
            gpu_device="/dev/dri/renderD128",
            hdr_fmt="SMPTE ST 2086",
        )
        vf = args[args.index("-vf") + 1]
        scale_idx = vf.find("scale_vaapi=")
        parity_idx = vf.find("scale=trunc(iw/2)*2")
        zscale_idx = vf.find("zscale=t=linear")
        assert scale_idx != -1 and parity_idx != -1 and zscale_idx != -1
        # scale_vaapi (GPU) → hwdownload → parity-fix scale (CPU, tiny)
        # → zscale tonemap on the 320x240 frame.
        assert scale_idx < parity_idx < zscale_idx
        assert "format=p010le" in vf

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    def test_cpu_path_retains_software_scale(self, mock_popen, mock_mediainfo, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu=None,
            gpu_device=None,
            hdr_fmt=None,
        )
        vf = args[args.index("-vf") + 1]
        # CPU path: plain libswscale scale — no GPU filters.
        assert "scale_cuda" not in vf
        assert "scale_vaapi" not in vf
        assert "hwdownload" not in vf
        assert "scale=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        # And no hwaccel_output_format leaked in.
        assert "-hwaccel_output_format" not in args

    @patch(
        "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
        return_value=VulkanProbeResult(device="vk", is_software=False),
    )
    @patch(
        "media_preview_generator.gpu.vulkan_probe.get_vulkan_env_overrides",
        return_value={},
    )
    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    def test_dv5_libplacebo_vf_unchanged(self, mock_popen, mock_mediainfo, _vk_env, _vk_info, mock_config):
        args = self._run(
            mock_mediainfo,
            mock_popen,
            mock_config,
            gpu="NVIDIA",
            gpu_device="cuda",
            hdr_fmt="Dolby Vision",
        )
        vf = args[args.index("-vf") + 1]
        # DV5 libplacebo chain: fps dropper runs FIRST (before hwupload)
        # so the decode→upload→tonemap pipeline only processes frames we
        # actually keep.  On NVIDIA Turing the old fps-inside-libplacebo
        # ordering exhausted the Vulkan allocator at 4K p010.
        assert vf.startswith("fps=fps=")
        # Frame drop happens on CPU frames, THEN they go to Vulkan via
        # hwupload and through libplacebo.
        assert "fps=fps=" in vf and "hwupload," in vf
        assert vf.index("fps=fps=") < vf.index("hwupload,")
        assert "libplacebo=tonemapping=" in vf
        # No fps inside the libplacebo filter anymore — it's upstream.
        assert ":fps=" not in vf
        assert "hwdownload,format=yuv420p" in vf
        assert "scale_cuda" not in vf, "DV5 libplacebo path must not use scale_cuda"
        # -hwaccel cuda is still set (NVDEC decodes the HEVC base), but
        # output format must NOT be cuda on this path.
        assert "-hwaccel" in args
        assert args[args.index("-hwaccel") + 1] == "cuda"
        assert "-hwaccel_output_format" not in args


CPU_SCALE = "scale=w=320:h=240:force_original_aspect_ratio=decrease"
ONE = 65536  # 1.0 in a display matrix's 16.16 fixed point


def _matrix(a: int, b: int, c: int, d: int) -> tuple[int, ...]:
    """A display matrix as ffprobe prints it for ``ffmpeg -display_rotation/-display_hflip/-display_vflip``."""
    return (a, b, 0, c, d, 0, 0, 0, 1 << 30)


def _ffprobe_side_data(matrix: tuple[int, ...] | None) -> str:
    """ffprobe's JSON for ``-show_entries stream_side_data=displaymatrix``, as it prints it."""
    if matrix is None:
        return json.dumps({"programs": [], "streams": [{}]})
    rows = "".join(
        f"{row:08x}: {matrix[row * 3]:11d} {matrix[row * 3 + 1]:11d} {matrix[row * 3 + 2]:11d}\n" for row in range(3)
    )
    return json.dumps({"programs": [], "streams": [{"side_data_list": [{"displaymatrix": f"\n{rows}"}]}]})


# Each display matrix ffmpeg writes (the same in MP4 and Matroska, measured with ffprobe 8.1), the filters FFmpeg's
# own autorotation puts in front of the CPU's filter graph for it, and the GPU's box: twice the thumbnail, as the frame
# is stored. ``-display_rotation`` is counter-clockwise.
TURNS = [
    pytest.param(_matrix(0, -ONE, ONE, 0), "transpose=cclock", "w=480:h=640", id="rotation-90"),
    pytest.param(_matrix(-ONE, 0, 0, -ONE), "hflip,vflip", "w=640:h=480", id="rotation-180"),
    pytest.param(_matrix(0, ONE, -ONE, 0), "transpose=clock", "w=480:h=640", id="rotation-270"),
    pytest.param(_matrix(-ONE, 0, 0, ONE), "hflip", "w=640:h=480", id="hflip"),
    # A lone vflip makes the filter before it write bottom-up, which hwdownload can't: the copy takes the download.
    pytest.param(_matrix(ONE, 0, 0, -ONE), "copy,vflip", "w=640:h=480", id="vflip"),
    pytest.param(_matrix(0, -ONE, -ONE, 0), "transpose=clock_flip", "w=480:h=640", id="rotation-90-hflip"),
    pytest.param(_matrix(0, ONE, ONE, 0), "transpose=cclock_flip", "w=480:h=640", id="rotation-270-hflip"),
]
SURFACE_GPUS = [
    pytest.param("NVIDIA", "cuda", "scale_cuda", id="cuda"),
    pytest.param("INTEL", "/dev/dri/renderD128", "scale_vaapi", id="intel-vaapi"),
    pytest.param("AMD", "/dev/dri/renderD128", "scale_vaapi", id="amd-vaapi"),
]


class TestDisplayMatrixTurn:
    """The filters for a display matrix are the ones FFmpeg's autorotation picks: the angle, then the mirroring."""

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    def test_each_matrix_gets_ffmpegs_own_filters(self, matrix, turn, box):
        from media_preview_generator.processing.filter_chain import rotation_filter

        assert rotation_filter(matrix) == turn

    @pytest.mark.parametrize(
        "matrix",
        [None, _matrix(ONE, 0, 0, ONE), (0,) * 9, (ONE, 0, 0), _matrix(46341, -46341, 46341, 46341)],
        ids=["none", "identity", "all-zero", "short", "45-degrees"],
    )
    def test_a_stream_shown_as_stored_or_at_an_angle_the_gpu_cant_mirror_gets_none(self, matrix):
        from media_preview_generator.processing.filter_chain import rotation_filter

        assert rotation_filter(matrix) is None

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    def test_the_matrix_is_read_from_ffprobes_side_data(self, matrix, turn, box):
        from media_preview_generator.processing.filter_chain import display_matrix

        assert display_matrix(json.loads(_ffprobe_side_data(matrix))["streams"][0]) == matrix

    @pytest.mark.parametrize(
        "stream",
        [
            {},
            {"side_data_list": None},
            {"side_data_list": 7},
            {"side_data_list": ["x", {"dv_profile": 5}]},
            {"side_data_list": [{"displaymatrix": "\n00000000: 1 2 x\n"}]},
            {"side_data_list": [{"displaymatrix": "\n00000000: 1 2 3\n"}]},
        ],
        ids=["no-side-data", "null", "number", "other-side-data", "not-numbers", "one-row"],
    )
    def test_a_stream_without_a_readable_matrix_has_none(self, stream):
        from media_preview_generator.processing.filter_chain import display_matrix

        assert display_matrix(stream) is None

    def test_the_matrix_after_other_side_data_is_still_found(self):
        from media_preview_generator.processing.filter_chain import display_matrix

        stream = json.loads(_ffprobe_side_data(_matrix(-ONE, 0, 0, ONE)))["streams"][0]
        stream["side_data_list"].insert(0, {"dv_profile": 8})
        assert display_matrix(stream) == _matrix(-ONE, 0, 0, ONE)


class TestDisplayTurn:
    """``generator.display_turn``: one ffprobe of the first video stream's display matrix, None when it can't say."""

    def test_asks_ffprobe_for_the_first_pictures_display_matrix_only(self):
        from media_preview_generator.processing.generator import display_turn

        done = subprocess.CompletedProcess([], 0, stdout=_ffprobe_side_data(_matrix(0, -ONE, -ONE, 0)))
        with patch("media_preview_generator.processing.generator.subprocess.run", return_value=done) as run:
            assert display_turn("/m/v.mkv") == "transpose=clock_flip"
        assert run.call_args.args[0] == [
            "ffprobe", "-v", "error", "-select_streams", "V:0", "-show_entries", "stream_side_data=displaymatrix",
            "-of", "json", "/m/v.mkv",
        ]  # fmt: skip

    @pytest.mark.parametrize(
        "outcome",
        [
            OSError("no ffprobe"),
            subprocess.TimeoutExpired("ffprobe", 30),
            subprocess.CompletedProcess([], 1, stdout=""),
            subprocess.CompletedProcess([], 0, stdout="not json"),
            subprocess.CompletedProcess([], 0, stdout="[]"),
            subprocess.CompletedProcess([], 0, stdout='{"streams": []}'),
            subprocess.CompletedProcess([], 0, stdout='{"streams": ["x"]}'),
        ],
        ids=["missing", "timeout", "failed", "not-json", "list", "no-video", "not-a-stream"],
    )
    def test_a_file_ffprobe_cant_read_is_taken_as_stored(self, outcome):
        from media_preview_generator.processing.generator import display_turn

        kwargs = {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome}
        with patch("media_preview_generator.processing.generator.subprocess.run", **kwargs):
            assert display_turn("/m/v.mkv") is None

    @pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
    @pytest.mark.parametrize("container", ["mp4", "mkv"])
    @pytest.mark.parametrize(
        "display",
        [["-display_rotation", "90"], ["-display_rotation", "180"], ["-display_rotation", "270"], ["-display_hflip"],
         ["-display_vflip"], ["-display_rotation", "90", "-display_hflip"], ["-display_rotation", "270", "-display_hflip"]],
        ids=["90", "180", "270", "hflip", "vflip", "90-hflip", "270-hflip"],
    )  # fmt: skip
    def test_the_turn_read_from_the_file_is_the_one_ffmpeg_makes_itself(self, container, display, tmp_path):
        """Real files and FFmpeg: the filters read from the file, with FFmpeg's own turn off, give the CPU path's
        thumbnail, pixel for pixel and at its size, for MP4 and for Matroska (which MediaInfo reads no angle from)."""
        from PIL import Image, ImageChops

        from media_preview_generator.processing.generator import display_turn

        ffmpeg = shutil.which("ffmpeg")
        stored = tmp_path / "stored.mp4"
        shown = tmp_path / f"shown.{container}"
        quiet = [ffmpeg, "-nostdin", "-loglevel", "error"]
        subprocess.run(
            [*quiet, "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=5:duration=1", "-c:v", "mpeg4", str(stored)],
            check=True,
            timeout=60,
        )
        subprocess.run([*quiet, *display, "-i", str(stored), "-c", "copy", str(shown)], check=True, timeout=60)
        turn = display_turn(str(shown))
        assert turn is not None
        theirs, ours = tmp_path / "ffmpeg.png", tmp_path / "ours.png"
        subprocess.run([*quiet, "-i", str(shown), "-frames:v", "1", "-vf", CPU_SCALE, str(theirs)], check=True)
        subprocess.run(
            [*quiet, "-noautorotate", "-i", str(shown), "-frames:v", "1", "-vf", f"{turn},{CPU_SCALE}", str(ours)],
            check=True,
        )

        with Image.open(theirs) as expected, Image.open(ours) as actual:
            assert actual.size == expected.size == ((135, 240) if "transpose" in turn else (320, 180))
            assert ImageChops.difference(actual.convert("RGB"), expected.convert("RGB")).getbbox() is None


class TestRotatedVideo:
    """FFmpeg turns and mirrors a stream's frames as its display matrix says only when they are in memory: the CPU's,
    and those of a GPU whose frames FFmpeg downloads (VideoToolbox, D3D11VA). Frames that stay on the GPU (CUDA, VAAPI)
    are turned after their download and sized by the CPU's scale, so a portrait 16:9 video's thumbnails are 135x240
    there too."""

    @staticmethod
    def _command(mock_config, gpu, device, matrix, *, hdr_fmt=None, transfer=None, probes=None) -> list[str]:
        info = MagicMock()
        info.video_tracks = [MagicMock(hdr_format=hdr_fmt, transfer_characteristics=transfer)]
        proc = MagicMock()
        proc.poll.side_effect = [None, 0]
        proc.returncode = 0

        def run(cmd, *args, **kwargs):
            if "stream_side_data=displaymatrix" in cmd:
                if probes is not None:
                    probes.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout=_ffprobe_side_data(matrix))
            return MagicMock(returncode=0)

        with (
            patch("media_preview_generator.processing.generator.MediaInfo") as mediainfo,
            patch("subprocess.Popen", return_value=proc) as popen,
            patch(
                "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
                return_value=VulkanProbeResult(device="Real GPU", is_software=False),
            ),
            patch("os.path.exists", return_value=False),
            patch("builtins.open", new_callable=mock_open),
            patch("time.sleep"),
            patch("media_preview_generator.processing.generator.glob.glob", return_value=[]),
            patch("subprocess.run", side_effect=run),
        ):
            mediainfo.parse.return_value = info
            generate_images("/test/v.mkv", "/tmp/o", gpu, device, mock_config)
        return popen.call_args_list[0][0][0]

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    @pytest.mark.parametrize(("gpu", "device", "scaler"), SURFACE_GPUS)
    @pytest.mark.parametrize(("hdr_fmt", "fmt"), [(None, "nv12"), ("SMPTE ST 2086", "p010le")], ids=["sdr", "hdr10"])
    def test_frames_kept_on_the_gpu_are_turned_after_download_and_sized_as_on_the_cpu(
        self, mock_config, gpu, device, scaler, matrix, turn, box, hdr_fmt, fmt
    ):
        args = self._command(mock_config, gpu, device, matrix, hdr_fmt=hdr_fmt)
        vf = args[args.index("-vf") + 1]

        assert args[args.index("-hwaccel_output_format") + 1] in {"cuda", "vaapi"}
        assert f"{scaler}={box}:force_original_aspect_ratio=decrease" in vf
        assert vf.index(f"hwdownload,format={fmt}") < vf.index(turn)
        assert vf.endswith(f",{turn},{CPU_SCALE}"), vf
        if hdr_fmt:
            assert vf.index("tonemap=hable") < vf.index(turn), "tone mapped on the even-sized frame, then turned"

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    def test_on_cuda_the_turn_follows_the_download_directly(self, mock_config, matrix, turn, box):
        args = self._command(mock_config, "NVIDIA", "cuda", matrix)

        assert args[args.index("-vf") + 1] == (
            f"fps=fps=0.2:round=up,scale_cuda={box}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
            f"format=nv12,hwdownload,format=nv12,{turn},{CPU_SCALE}"
        )

    @pytest.mark.parametrize(
        "matrix", [None, _matrix(ONE, 0, 0, ONE), _matrix(46341, -46341, 46341, 46341)], ids=["none", "identity", "45"]
    )
    @pytest.mark.parametrize(("gpu", "device", "scaler"), SURFACE_GPUS)
    def test_a_stream_shown_as_stored_keeps_its_gpu_scale(self, mock_config, gpu, device, scaler, matrix):
        args = self._command(mock_config, gpu, device, matrix)
        vf = args[args.index("-vf") + 1]

        assert f"{scaler}=w=320:h=240:force_original_aspect_ratio=decrease" in vf
        assert "transpose" not in vf and "flip" not in vf
        assert CPU_SCALE not in vf

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    @pytest.mark.parametrize(
        ("gpu", "device"),
        [(None, None), ("APPLE", None), ("WINDOWS_GPU", None)],
        ids=["cpu", "videotoolbox", "d3d11va"],
    )
    def test_frames_in_memory_are_left_to_ffmpegs_own_turn(self, mock_config, gpu, device, matrix, turn, box):
        args = self._command(mock_config, gpu, device, matrix)
        vf = args[args.index("-vf") + 1]

        assert "-hwaccel_output_format" not in args
        assert vf.endswith(f",{CPU_SCALE}") and turn not in vf, vf

    @pytest.mark.parametrize("gpu", [None, "APPLE", "WINDOWS_GPU"], ids=["cpu", "videotoolbox", "d3d11va"])
    def test_a_worker_whose_frames_are_in_memory_doesnt_probe_the_turn(self, mock_config, gpu):
        probes: list[list[str]] = []
        self._command(mock_config, gpu, None, _matrix(0, -ONE, ONE, 0), probes=probes)

        assert probes == []

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    @pytest.mark.parametrize(
        ("gpu", "device", "download"),
        [("INTEL", "/dev/dri/renderD128", "hwdownload,format=nv12,format=yuv420p"),
         ("AMD", "/dev/dri/renderD128", "hwdownload,format=yuv420p")],
        ids=["intel-opencl", "amd-vulkan"],
    )  # fmt: skip
    def test_dolby_vision_profile_5_on_vaapi_is_turned_after_its_download(
        self, mock_config, gpu, device, download, matrix, turn, box
    ):
        args = self._command(mock_config, gpu, device, matrix, hdr_fmt="Dolby Vision")
        vf = args[args.index("-vf") + 1]

        assert vf.endswith(f"{download},{turn},{CPU_SCALE}"), vf

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    def test_dolby_vision_profile_5_on_nvidia_uploads_frames_ffmpeg_turned(self, mock_config, matrix, turn, box):
        args = self._command(mock_config, "NVIDIA", "cuda", matrix, hdr_fmt="Dolby Vision")
        vf = args[args.index("-vf") + 1]

        assert "-hwaccel_output_format" not in args
        assert "hwupload" in vf and turn not in vf, vf

    @staticmethod
    def _retried_commands(mock_config, gpu, device, matrix, hdr_fmt) -> list[list[str]]:
        """Every FFmpeg command of a file whose every run fails with a tone-mapping error, through each retry tier to
        the CPU hand-off."""
        info = MagicMock()
        info.video_tracks = [MagicMock(hdr_format=hdr_fmt, transfer_characteristics=None)]

        def failed_run(*_args, **_kwargs):
            proc = MagicMock()
            proc.poll.side_effect = [None, 1]
            proc.returncode = 1
            return proc

        def run(cmd, *args, **kwargs):
            if "stream_side_data=displaymatrix" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=_ffprobe_side_data(matrix))
            return MagicMock(returncode=0)

        with (
            patch("media_preview_generator.processing.generator.MediaInfo") as mediainfo,
            patch("subprocess.Popen", side_effect=failed_run) as popen,
            patch(
                "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
                return_value=VulkanProbeResult(device="Real GPU", is_software=False),
            ),
            patch("os.path.exists", return_value=False),
            patch("builtins.open", new_callable=mock_open),
            patch("time.sleep"),
            patch("media_preview_generator.processing.generator.glob.glob", return_value=[]),
            patch("subprocess.run", side_effect=run),
            patch(
                "media_preview_generator.processing.generator.classify_dv_safe_retry_reason",
                return_value="Tone mapping failed",
            ),
        ):
            mediainfo.parse.return_value = info
            with pytest.raises(CodecNotSupportedError):
                generate_images("/test/v.mkv", "/tmp/o", gpu, device, mock_config)
        return [call.args[0] for call in popen.call_args_list]

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    @pytest.mark.parametrize(
        ("gpu", "device"), [("NVIDIA", "cuda"), ("INTEL", "/dev/dri/renderD128")], ids=["cuda", "vaapi"]
    )
    def test_the_dv_safe_retry_keeps_the_turn_once(self, mock_config, gpu, device, matrix, turn, box):
        commands = self._retried_commands(mock_config, gpu, device, matrix, "SMPTE ST 2086")
        retry_vf = commands[-1][commands[-1].index("-vf") + 1]

        assert len(commands) >= 2 and "tonemap" not in retry_vf
        assert retry_vf.endswith(f"hwdownload,format=nv12,{turn},{CPU_SCALE}") or retry_vf.endswith(
            f"hwdownload,format=nv12,scale=trunc(iw/2)*2:trunc(ih/2)*2,{turn},{CPU_SCALE}"
        ), retry_vf
        assert all(command[command.index("-vf") + 1].count(turn) == 1 for command in commands)

    @pytest.mark.parametrize(("matrix", "turn", "box"), TURNS)
    def test_dolby_vision_profile_5s_software_retry_leaves_the_turn_to_ffmpeg(self, mock_config, matrix, turn, box):
        commands = self._retried_commands(mock_config, "INTEL", "/dev/dri/renderD128", matrix, "Dolby Vision")
        software = [command for command in commands if "-hwaccel" not in command]
        vfs = [command[command.index("-vf") + 1] for command in commands]

        assert len(software) == 1, "the software decode + libplacebo tier ran"
        software_vf = software[0][software[0].index("-vf") + 1]
        assert "libplacebo" in software_vf and turn not in software_vf, "its frames are in memory: FFmpeg turns them"
        assert vfs[0].endswith(f",{turn},{CPU_SCALE}"), "the OpenCL tier downloads surfaces and turns them"
        assert vfs[-1].endswith(f",{turn},{CPU_SCALE}") and "tonemap" not in vfs[-1], "the DV-safe tier too"


class TestFfmpegThreadFlags:
    """Test that FFmpeg thread cap flags are applied correctly."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_gpu_path_includes_thread_cap(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """GPU processing should include -threads and -filter_threads flags."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        mock_exists.return_value = False
        mock_glob.return_value = []

        mock_config.ffmpeg_threads = 2
        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda", mock_config)

        args = mock_popen.call_args[0][0]
        assert "-threads" in args
        thread_idx = args.index("-threads")
        assert args[thread_idx + 1] == "2"
        assert "-filter_threads" in args
        ft_idx = args.index("-filter_threads")
        assert args[ft_idx + 1] == "2"
        # HW decode is active (NVIDIA/cuda) so the video decoder thread cap
        # should still be emitted to prevent oversubscription across GPU workers.
        assert "-threads:v" in args
        tv_idx = args.index("-threads:v")
        assert args[tv_idx + 1] == "1"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_cpu_path_omits_thread_cap(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """CPU-only processing must omit both -threads and -threads:v so software
        decode can use all available cores (regression guard for issue #212)."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        mock_exists.return_value = False
        mock_glob.return_value = []

        mock_config.ffmpeg_threads = 2
        generate_images("/test/video.mp4", temp_dir, None, None, mock_config)

        args = mock_popen.call_args[0][0]
        # No hardware decode on a pure CPU path, so neither the global -threads
        # cap nor the per-stream -threads:v video-decoder cap should be set.
        assert "-threads:v" not in args, "CPU path should not cap the video decoder — forces single-threaded SW decode"
        bare_threads = [i for i, a in enumerate(args) if a == "-threads"]
        assert len(bare_threads) == 0, "CPU path should not have global -threads cap"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("glob.glob")
    def test_gpu_path_zero_threads_omits_cap(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """GPU processing with ffmpeg_threads=0 should omit -threads (auto)."""
        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc
        mock_exists.return_value = False
        mock_glob.return_value = []

        mock_config.ffmpeg_threads = 0
        generate_images("/test/video.mp4", temp_dir, "NVIDIA", "cuda", mock_config)

        args = mock_popen.call_args[0][0]
        bare_threads = [i for i, a in enumerate(args) if a == "-threads"]
        assert len(bare_threads) == 0, "ffmpeg_threads=0 should omit global -threads"


class TestCancellation:
    """Test that cancellation kills FFmpeg and skips retries."""

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    def test_cancel_kills_ffmpeg_process(
        self,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancel_check terminates the running FFmpeg process."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                None,
                None,
                mock_config,
                cancel_check=lambda: True,
            )

        mock_proc.terminate.assert_called_once()

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    def test_cancel_skips_skip_frame_retry(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancellation after first FFmpeg failure skips the skip-frame retry."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        cancelled = False

        def cancel_after_first():
            nonlocal cancelled
            if mock_popen.call_count >= 1:
                cancelled = True
            return cancelled

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                None,
                None,
                mock_config,
                cancel_check=cancel_after_first,
            )

        assert mock_popen.call_count == 1

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    def test_cancel_skips_dv_safe_retry(
        self,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancellation after skip-frame retry skips the DV-safe retry."""
        mock_run.return_value = MagicMock(returncode=1)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format="Dolby Vision / SMPTE ST 2086", transfer_characteristics="PQ")]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 1
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False
        mock_glob.return_value = []

        cancel_flag = False

        def cancel_after_first_ffmpeg():
            nonlocal cancel_flag
            if mock_popen.call_count >= 1:
                cancel_flag = True
            return cancel_flag

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                "NVIDIA",
                None,
                mock_config,
                cancel_check=cancel_after_first_ffmpeg,
            )

        assert mock_popen.call_count == 1

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_cancel_skips_gpu_to_cpu_fallback(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Test that cancellation prevents CodecNotSupportedError (no CPU fallback)."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc_skip = MagicMock()
        mock_proc_skip.poll.side_effect = [None, 0]
        mock_proc_skip.returncode = 69

        mock_proc_noskip = MagicMock()
        mock_proc_noskip.poll.side_effect = [None, 0]
        mock_proc_noskip.returncode = 69

        mock_popen.side_effect = [mock_proc_skip, mock_proc_noskip]
        mock_exists.return_value = False
        mock_glob.return_value = []
        mock_detect.return_value = True

        cancel_flag = False

        def cancel_after_retries():
            nonlocal cancel_flag
            if mock_popen.call_count >= 2:
                cancel_flag = True
            return cancel_flag

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                "NVIDIA",
                None,
                mock_config,
                cancel_check=cancel_after_retries,
            )

        assert mock_popen.call_count == 2

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    def test_cancel_after_pause_resumes_before_terminating(
        self,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Cancel while paused must SIGCONT BEFORE terminate(), otherwise the
        SIGTERM is queued behind SIGSTOP and the FFmpeg process hangs forever.

        Production handles this at ffmpeg_runner.py line ~485-491:
        ``if paused_locally: proc.send_signal(SIGCONT) … proc.terminate()``.
        Without this ordering, a user who clicks Pause then Cancel ends up
        with FFmpeg silently stuck and the worker slot wedged for the rest
        of the run. This test pins the ordering: SIGCONT MUST precede
        terminate when the process was paused at the moment of cancel.
        """
        import signal as _signal

        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # never exits on its own
        mock_proc.wait.return_value = None
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False

        # First call to pause_check is True (so SIGSTOP fires); subsequent
        # calls are False (so we leave the pause-branch on the next loop).
        # cancel_check fires True on the second iteration so we hit cancel
        # while paused_locally is True.
        pause_calls = {"n": 0}

        def pause_check():
            pause_calls["n"] += 1
            return pause_calls["n"] == 1  # True only on first call

        cancel_calls = {"n": 0}

        def cancel_check():
            cancel_calls["n"] += 1
            return cancel_calls["n"] >= 2  # cancel on the second loop

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                None,
                None,
                mock_config,
                cancel_check=cancel_check,
                pause_check=pause_check,
            )

        # Verify the SIGCONT-before-terminate ordering in the call stream.
        sigcont_call_indices: list[int] = []
        terminate_call_indices: list[int] = []
        for idx, call in enumerate(mock_proc.method_calls):
            if call[0] == "send_signal" and call[1] and call[1][0] == _signal.SIGCONT:
                sigcont_call_indices.append(idx)
            if call[0] == "terminate":
                terminate_call_indices.append(idx)

        assert sigcont_call_indices, (
            "SIGCONT was never sent — paused FFmpeg can't receive SIGTERM and would hang forever"
        )
        assert terminate_call_indices, "terminate() was never called after cancel"
        assert sigcont_call_indices[0] < terminate_call_indices[0], (
            "SIGCONT must precede terminate(); otherwise SIGTERM queues behind SIGSTOP and FFmpeg hangs"
        )

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    def test_cancel_falls_back_to_kill_when_terminate_times_out(
        self,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """If FFmpeg ignores SIGTERM for 5s, we MUST escalate to SIGKILL.

        Production at ffmpeg_runner.py line ~493-497:
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait()

        Without this fallback, a stuck FFmpeg (e.g. blocked on broken
        hardware decoder) leaks a worker slot and a GPU context until
        the entire app restarts. This test pins the kill() escalation
        so a refactor that drops the timeout/except branch is caught.
        """
        import subprocess as _subprocess

        mock_run.return_value = MagicMock(returncode=0)
        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        # First wait() (after terminate) raises TimeoutExpired to force
        # the kill escalation; second wait() (after kill) returns cleanly.
        mock_proc.wait.side_effect = [_subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5), None]
        mock_popen.return_value = mock_proc

        mock_exists.return_value = False

        with pytest.raises(CancellationError):
            generate_images(
                "/test/video.mp4",
                temp_dir,
                None,
                None,
                mock_config,
                cancel_check=lambda: True,
            )

        mock_proc.terminate.assert_called_once()
        (
            mock_proc.kill.assert_called_once(),
            ("terminate() timed out → kill() escalation missing; FFmpeg would leak indefinitely"),
        )
        # And the wait() after kill MUST be called (otherwise we leak a zombie).
        assert mock_proc.wait.call_count >= 2, "wait() must be called again after kill() to reap the zombie"


class TestFailureScope:
    """Per-job failure scoping — verifies concurrent jobs can't cross-contaminate
    each other's failure summaries.  Regression guard for the bug where the
    global ``_failures`` list caused one job's summary to include another
    job's errors.
    """

    def test_record_outside_scope_is_dropped(self):
        """Records written with no active scope are logged and discarded."""
        record_failure("/tmp/stray.mkv", 1, "noise", worker_type="GPU")
        assert get_failures() == []

    def test_scope_isolates_records_per_job(self):
        """Records written inside scope A must not appear inside scope B."""
        with failure_scope("job-A"):
            record_failure("/tmp/a.mkv", 1, "A failure", worker_type="GPU")
            assert [f["file"] for f in get_failures()] == ["/tmp/a.mkv"]
            clear_failures()

        with failure_scope("job-B"):
            assert get_failures() == []
            record_failure("/tmp/b.mkv", 2, "B failure", worker_type="CPU")
            assert [f["file"] for f in get_failures()] == ["/tmp/b.mkv"]
            clear_failures()

    def test_concurrent_scopes_in_different_threads(self):
        """Two threads inside two scopes never see each other's records.

        Simulates the real bug: job A's worker thread records failures
        concurrently with job B's job-runner thread calling ``get_failures``.
        """
        import threading

        ready_a = threading.Event()
        ready_b = threading.Event()
        proceed = threading.Event()
        results: dict = {}

        def thread_a():
            with failure_scope("job-A"):
                record_failure("/tmp/a.mkv", 1, "A failure")
                ready_a.set()
                proceed.wait()
                results["A"] = get_failures()
                clear_failures()

        def thread_b():
            with failure_scope("job-B"):
                record_failure("/tmp/b.mkv", 2, "B failure")
                ready_b.set()
                proceed.wait()
                results["B"] = get_failures()
                clear_failures()

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ready_a.wait()
        ready_b.wait()
        proceed.set()
        ta.join()
        tb.join()

        assert [f["file"] for f in results["A"]] == ["/tmp/a.mkv"]
        assert [f["file"] for f in results["B"]] == ["/tmp/b.mkv"]

    def test_scope_nested_same_job_shares_bucket(self):
        """Re-entering the same job_id on a different call site (e.g. a
        worker thread vs the dispatcher thread) must operate on the same
        shared bucket so the dispatcher's ``get_failures`` sees what the
        worker recorded.
        """
        import threading

        def worker():
            with failure_scope("job-X"):
                record_failure("/tmp/x.mkv", 99, "worker-side", worker_type="GPU")

        with failure_scope("job-X"):
            t = threading.Thread(target=worker)
            t.start()
            t.join()
            failures = get_failures()
            assert [f["file"] for f in failures] == ["/tmp/x.mkv"]
            clear_failures()

    def test_clear_failures_only_drops_current_scope(self):
        """clear_failures must only drop the current scope's bucket,
        leaving records for other concurrently-tracked jobs intact.
        """
        with failure_scope("job-keep"):
            record_failure("/tmp/keep.mkv", 1, "keep me")

        with failure_scope("job-drop"):
            record_failure("/tmp/drop.mkv", 1, "drop me")
            clear_failures()
            assert get_failures() == []

        with failure_scope("job-keep"):
            assert [f["file"] for f in get_failures()] == ["/tmp/keep.mkv"]
            clear_failures()


class TestSkipFrameInitialDefaults:
    """Test that ``-skip_frame:v nokey`` is applied to the first FFmpeg
    attempt on every path except DV Profile 5 / libplacebo.

    Regression guard for issue #216: a previous preflight probe gave
    false negatives on benign Dolby Vision RPU parsing artifacts and
    caused ~20x slowdowns on DV Profile 8.x content.  The probe has been
    removed; the retry cascade handles genuine skip_frame failures.
    """

    def _setup_success_mocks(
        self,
        hdr_format_str,
        mock_run,
        mock_popen,
        mock_mediainfo,
        mock_exists,
        mock_file,
        mock_detect,
        mock_glob,
        temp_dir,
        transfer_characteristics=None,
    ):
        """Wire mocks so a single FFmpeg Popen call returns success."""
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [
            MagicMock(hdr_format=hdr_format_str, transfer_characteristics=transfer_characteristics)
        ]
        mock_mediainfo.parse.return_value = mock_info

        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_sdr_first_attempt_uses_skip_frame(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """SDR content (no HDR format) must attempt -skip_frame:v nokey
        on the first FFmpeg invocation.
        """
        self._setup_success_mocks(
            None,
            mock_run,
            mock_popen,
            mock_mediainfo,
            mock_exists,
            mock_file,
            mock_detect,
            mock_glob,
            temp_dir,
        )

        generate_images("/test/video.mkv", temp_dir, None, None, mock_config)

        assert mock_popen.call_count == 1
        args = mock_popen.call_args_list[0][0][0]
        assert "-skip_frame:v" in args
        assert args[args.index("-skip_frame:v") + 1] == "nokey"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_dv_profile8_hdr10_first_attempt_uses_skip_frame(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """Dolby Vision Profile 8.x with HDR10 fallback must attempt
        -skip_frame:v nokey on the first FFmpeg invocation.

        This is the exact profile from issue #216: the reporter's slow
        file was DV Profile 8.6 BL+RPU / HDR10 compatible, and the old
        probe rejected skip_frame on benign RPU parsing artifacts,
        costing ~20x speed.
        """
        self._setup_success_mocks(
            "Dolby Vision / SMPTE ST 2086",
            mock_run,
            mock_popen,
            mock_mediainfo,
            mock_exists,
            mock_file,
            mock_detect,
            mock_glob,
            temp_dir,
            transfer_characteristics="PQ",
        )

        generate_images("/test/video.mkv", temp_dir, None, None, mock_config)

        assert mock_popen.call_count == 1
        args = mock_popen.call_args_list[0][0][0]
        assert "-skip_frame:v" in args
        assert args[args.index("-skip_frame:v") + 1] == "nokey"

    @patch("media_preview_generator.processing.generator.MediaInfo")
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    @patch("media_preview_generator.processing.generator.os.rename")
    @patch("media_preview_generator.processing.generator.os.remove")
    @patch("os.path.exists")
    @patch("builtins.open", new_callable=mock_open)
    @patch("time.sleep")
    @patch("media_preview_generator.processing.generator.glob.glob")
    @patch("media_preview_generator.processing.generator._detect_codec_error")
    def test_retry_drops_skip_frame_when_first_attempt_fails(
        self,
        mock_detect,
        mock_glob,
        mock_sleep,
        mock_file,
        mock_exists,
        mock_remove,
        mock_rename,
        mock_run,
        mock_popen,
        mock_mediainfo,
        temp_dir,
        mock_config,
    ):
        """If the first FFmpeg attempt returns non-zero, a second call
        must run without -skip_frame:v. This exercises the retry path
        that makes removal of the preflight probe safe.
        """
        mock_run.return_value = MagicMock(returncode=0)

        mock_info = MagicMock()
        mock_info.video_tracks = [MagicMock(hdr_format=None)]
        mock_mediainfo.parse.return_value = mock_info

        first_proc = MagicMock()
        first_proc.poll.side_effect = [None, 0]
        first_proc.returncode = 1

        retry_proc = MagicMock()
        retry_proc.poll.side_effect = [None, 0]
        retry_proc.returncode = 0

        mock_popen.side_effect = [first_proc, retry_proc]
        mock_exists.return_value = True
        mock_file.return_value.readlines.return_value = []
        mock_detect.return_value = False

        img1 = f"{temp_dir}/img-000001.jpg"
        ts1 = f"{temp_dir}/0000000000.jpg"

        def glob_side_effect(pattern):
            if "img*.jpg" in pattern:
                return [img1]
            if pattern.endswith("*.jpg"):
                return [ts1]
            return []

        mock_glob.side_effect = glob_side_effect

        generate_images("/test/video.mkv", temp_dir, None, None, mock_config)

        assert mock_popen.call_count == 2
        first_args = mock_popen.call_args_list[0][0][0]
        retry_args = mock_popen.call_args_list[1][0][0]
        assert "-skip_frame:v" in first_args
        assert "-skip_frame:v" not in retry_args


class TestBuildDV5Vf:
    """Contract tests for the unified DV5 filter-chain builder.

    Each assertion is the exact string the three inline assemblies
    produced prior to extraction — byte-for-byte — so the refactor
    is demonstrably a pure code-motion change.
    """

    _BASE_SCALE = "scale=w=320:h=240:force_original_aspect_ratio=decrease"

    def test_intel_opencl_chain_is_byte_identical(self):
        got = build_dv5_vf(
            path_kind=DV5_PATH_INTEL_OPENCL,
            tonemap_algorithm="reinhard",
            fps_value=0.5,
            base_scale=self._BASE_SCALE,
        )
        assert got == (
            "fps=fps=0.5:round=up,"
            "setparams=color_primaries=bt2020:"
            "color_trc=smpte2084:colorspace=bt2020nc,"
            "hwmap=derive_device=opencl:mode=read,"
            "tonemap_opencl=format=nv12:p=bt709:t=bt709:"
            "m=bt709:tonemap=reinhard"
            ":peak=100:desat=0,"
            f"hwdownload,format=nv12,format=yuv420p,{self._BASE_SCALE}"
        )

    def test_vaapi_vulkan_chain_is_byte_identical(self):
        got = build_dv5_vf(
            path_kind=DV5_PATH_VAAPI_VULKAN,
            tonemap_algorithm="reinhard",
            fps_value=0.5,
            base_scale=self._BASE_SCALE,
        )
        assert got == (
            "fps=fps=0.5:round=up,"
            "hwmap=derive_device=vulkan,"
            "libplacebo=tonemapping=reinhard"
            ":format=yuv420p"
            ":contrast=1.3:saturation=1.3,"
            f"hwdownload,format=yuv420p,{self._BASE_SCALE}"
        )

    def test_libplacebo_hwupload_chain_is_byte_identical(self):
        got = build_dv5_vf(
            path_kind=DV5_PATH_LIBPLACEBO,
            tonemap_algorithm="reinhard",
            fps_value=0.5,
            base_scale=self._BASE_SCALE,
        )
        assert got == (
            "fps=fps=0.5:round=up,"
            "hwupload,"
            "libplacebo=tonemapping=reinhard"
            ":format=yuv420p"
            ":contrast=1.3:saturation=1.3,"
            f"hwdownload,format=yuv420p,{self._BASE_SCALE}"
        )

    def test_unknown_path_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown DV5 path_kind"):
            build_dv5_vf(
                path_kind="not_a_real_path",
                tonemap_algorithm="reinhard",
                fps_value=0.5,
                base_scale=self._BASE_SCALE,
            )

    def test_fps_appears_first_in_every_variant(self):
        """fps-first ordering is required on NVIDIA Turing (VK OOM fix)
        and significantly faster on Intel OpenCL.  All three variants
        must place the fps dropper at the head of the chain."""
        for path in (
            DV5_PATH_INTEL_OPENCL,
            DV5_PATH_VAAPI_VULKAN,
            DV5_PATH_LIBPLACEBO,
        ):
            got = build_dv5_vf(
                path_kind=path,
                tonemap_algorithm="reinhard",
                fps_value=0.5,
                base_scale=self._BASE_SCALE,
            )
            assert got.startswith("fps=fps=0.5:round=up,"), path
