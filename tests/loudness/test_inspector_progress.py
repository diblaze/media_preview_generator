"""Canonical active slots survive concurrency and always clear on handler exit."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from media_preview_generator.loudness import job
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web.jobs import Job, JobManager, JobProgress


def test_concurrent_duplicate_paths_remove_one_slot_at_a_time(tmp_path) -> None:
    manager = JobManager(config_dir=str(tmp_path))
    entry = manager.create_job(kind="loudness", library_name="Concurrent tracks")
    barrier = threading.Barrier(3)
    release = threading.Event()
    first_done = threading.Event()

    def slot(first: bool) -> None:
        manager.update_progress(entry.id, file_started="/media/same.mkv")
        barrier.wait(timeout=5)
        if not first:
            assert release.wait(5)
        manager.update_progress(entry.id, file_finished="/media/same.mkv")
        if first:
            first_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            tasks = [executor.submit(slot, first) for first in (True, False)]
            barrier.wait(timeout=5)
            assert first_done.wait(5)
            assert entry.progress.current_files == ["/media/same.mkv"]
            release.set()
            for task in tasks:
                task.result(timeout=5)
        assert entry.progress.current_files == []
    finally:
        release.set()
        manager.close()


@pytest.mark.parametrize("handler", ["check_fn", "process_fn"])
def test_handler_exception_clears_its_current_file(tmp_path, monkeypatch, handler: str) -> None:
    manager = JobManager(config_dir=str(tmp_path))
    entry = manager.create_job(kind="loudness", library_name="Failure cleanup")

    def fail(*args, **kwargs):
        assert entry.progress.current_files == ["/media/failure.mkv"]
        raise OSError("file vanished")

    monkeypatch.setattr(job, "check_item" if handler == "check_fn" else "process_item", fail)

    def activity(path: str, started: bool) -> None:
        manager.update_progress(entry.id, **{"file_started" if started else "file_finished": path})

    handlers = job.kind_handlers(job.LoudnessContext(registry=Mock(), ffmpeg="ffmpeg"), active_files_callback=activity)
    try:
        with pytest.raises(OSError, match="file vanished"):
            getattr(handlers, handler)(ProcessableItem(canonical_path="/media/failure.mkv", server_id=""))
        assert entry.progress.current_files == []
    finally:
        manager.close()


def test_progress_deserializes_old_and_new_snapshots() -> None:
    assert Job(id="old", progress={"percent": 25}).progress.current_files == []
    snapshot = JobProgress(current_files=["/media/one.mkv", "/media/two.mkv"]).to_dict()
    restored = Job(id="new", progress=snapshot)
    assert restored.progress.current_files == ["/media/one.mkv", "/media/two.mkv"]
