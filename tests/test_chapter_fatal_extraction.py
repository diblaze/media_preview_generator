"""A stalled or corrupt source must not consume one watchdog interval per chapter."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters
from media_preview_generator.servers.plex_chapters import Chapter, ChapterTarget

CORRUPTION = "[matroska,webm @ 0x1234] 0x00 at pos 3843235529 invalid as first byte of an EBML number"


@pytest.fixture
def extraction(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"stable source")
    target = ChapterTarget(
        1,
        2,
        3,
        calculate_plex_hash(source),
        str(source),
        source.stat().st_size,
        100,
        tuple(Chapter(i + 1, i * 1000, (i + 1) * 1000, "", i + 10, 20) for i in range(3)),
        "machine",
    )
    plan = chapters.ChapterPlan(
        MagicMock(), str(source), get_source_fingerprint(source), tmp_path / "Chapters", {}, target
    )
    track = SimpleNamespace(duration=10000, transfer_characteristics=None, hdr_format=None)
    media = SimpleNamespace(video_tracks=[track])
    monkeypatch.setattr(chapters.MediaInfo, "parse", lambda _path: media)
    config = SimpleNamespace(tonemap_algorithm="hable", ffmpeg_threads=2, thumbnail_quality=2)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)
    return plan, config, media, register


@pytest.mark.parametrize(
    "stderr,retryable",
    [
        ([chapters.STALL_WATCHDOG_LINE], True),
        ([CORRUPTION], False),
        ([CORRUPTION, chapters.STALL_WATCHDOG_LINE], False),
    ],
)
def test_fatal_source_stop_preserves_completed_images_and_resume_skips_them(extraction, monkeypatch, stderr, retryable):
    plan, config, _media, register = extraction

    def factory(**options):
        def run(**_kwargs):
            if options["chapter_start_ms"] == 1000:
                return -9, 0, "", stderr
            Image.new("RGB", (1280, 720)).save(options["chapter_output"], "JPEG")
            return 0, 0, "", []

        return run

    runner = MagicMock(side_effect=factory)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    progress = []
    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)
    assert (result.status, result.completed, result.total, result.retryable) == ("failed", 1, 3, retryable)
    assert "remaining chapter attempts stopped" in result.message
    assert [c.kwargs["chapter_start_ms"] for c in runner.call_args_list] == [0, 1000]
    assert progress[-1] == {"stage": "failed", "processed": 2, "total": 3, "ready": 1, "failed": 1}
    register.assert_not_called()
    assert set(chapters._fresh_images(plan)) == {"1"}
    ready_bytes = (plan.folder / "chapter1.jpg").read_bytes()
    runner.reset_mock()
    chapters.publish_chapters(plan, config)
    assert [c.kwargs["chapter_start_ms"] for c in runner.call_args_list] == [1000]
    assert (plan.folder / "chapter1.jpg").read_bytes() == ready_bytes
    assert not (plan.folder / "chapter3.jpg").exists()


@pytest.mark.parametrize("stderr", [[chapters.STALL_WATCHDOG_LINE], [CORRUPTION]])
def test_fatal_error_cannot_trigger_near_eof_fallback(extraction, monkeypatch, stderr):
    plan, config, media, _register = extraction
    runner = MagicMock(return_value=lambda **_kwargs: (-9, 0, "", [*stderr, "No filtered frames"]))
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    with pytest.raises((chapters.ChapterExtractionStalledError, chapters.ChapterSourceCorruptionError)):
        chapters.extract_chapter_frame(plan.canonical_path, 10000, plan.folder / "frame.jpg", config, media_info=media)
    assert runner.call_count == 1


@pytest.mark.parametrize(
    "returncode,stderr",
    [(234, ["No filtered frames"]), (234, ["File ended prematurely"]), (-9, [])],
)
def test_individual_frame_failure_does_not_abort_other_chapters(extraction, monkeypatch, returncode, stderr):
    plan, config, _media, register = extraction

    def factory(**options):
        def run(**_kwargs):
            if options["chapter_start_ms"] == 1000:
                return returncode, 0, "", stderr
            Image.new("RGB", (1280, 720)).save(options["chapter_output"], "JPEG")
            return 0, 0, "", []

        return run

    runner = MagicMock(side_effect=factory)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    result = chapters.publish_chapters(plan, config)
    assert (result.status, result.completed, result.total, result.retryable) == ("failed", 2, 3, False)
    assert runner.call_count == 3
    assert set(chapters._fresh_images(plan)) == {"1", "3"}
    register.assert_not_called()


def test_watchdog_during_endpoint_fallback_still_aborts(extraction, monkeypatch):
    plan, config, media, _register = extraction
    runner = MagicMock(
        side_effect=[
            lambda **_kwargs: (234, 0, "", ["No filtered frames"]),
            lambda **_kwargs: (-9, 0, "", [chapters.STALL_WATCHDOG_LINE]),
        ]
    )
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    with pytest.raises(chapters.ChapterExtractionStalledError):
        chapters.extract_chapter_frame(plan.canonical_path, 10000, plan.folder / "frame.jpg", config, media_info=media)
    assert runner.call_count == 2


def test_successful_frame_is_accepted_despite_demux_warning(extraction, monkeypatch):
    plan, config, media, _register = extraction
    output = plan.folder.parent / "frame.jpg"

    def run(**_kwargs):
        Image.new("RGB", (1280, 720)).save(output, "JPEG")
        return 0, 0, "", [CORRUPTION]

    monkeypatch.setattr(chapters, "create_ffmpeg_runner", lambda **_kwargs: run)
    chapters.extract_chapter_frame(plan.canonical_path, 0, output, config, media_info=media)
    assert output.exists()
