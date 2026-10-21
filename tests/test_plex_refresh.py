"""Background Plex notification capacity, coalescing and Analyze API contracts."""

from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from media_preview_generator.processing.plex_refresh import PlexRefreshQueue
from media_preview_generator.servers.plex import PlexServer


def test_queue_coalesces_pending_hints_and_rejects_overflow():
    started, release, finished = Event(), Event(), Event()
    calls = []

    def refresh(path, item_id):
        calls.append((path, item_id))
        if path == "/media/a.mkv":
            started.set()
            assert release.wait(2)
        else:
            finished.set()

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(capacity=1)
    try:
        assert queue.enqueue(server, "/media/a.mkv", None)
        assert started.wait(2)
        assert queue.enqueue(server, "/media/b.mkv", None)
        assert queue.enqueue(server, "/media/b.mkv", "22")
        assert queue.enqueue(server, "/media/b.mkv", None)
        assert not queue.enqueue(server, "/media/c.mkv", "33")
        # The first request is still blocked, so enqueuing never waited for Plex.
        assert not release.is_set()
        release.set()
        assert finished.wait(2)
        assert calls == [("/media/a.mkv", None), ("/media/b.mkv", "22")]
    finally:
        release.set()
        queue.close()


def test_write_during_inflight_notification_schedules_another_pass():
    started, release, finished = Event(), Event(), Event()
    calls = []

    def refresh(path, item_id):
        calls.append((path, item_id))
        if len(calls) == 1:
            started.set()
            assert release.wait(2)
        else:
            finished.set()

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue()
    try:
        assert queue.enqueue(server, "/media/a.mkv", "1")
        assert started.wait(2)
        assert queue.enqueue(server, "/media/a.mkv", "1")
        release.set()
        assert finished.wait(2)
        assert calls == [("/media/a.mkv", "1"), ("/media/a.mkv", "1")]
    finally:
        release.set()
        queue.close()


def test_offline_notification_does_not_stop_following_work():
    finished = Event()
    calls = []

    def refresh(path, item_id):
        calls.append((path, item_id))
        if item_id == "1":
            raise ConnectionError("offline")
        finished.set()

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue()
    try:
        assert queue.enqueue(server, "/media/a.mkv", "1")
        assert queue.enqueue(server, "/media/b.mkv", "2")
        assert finished.wait(2)
        assert calls == [("/media/a.mkv", "1"), ("/media/b.mkv", "2")]
    finally:
        queue.close()
    assert not queue.enqueue(server, "/media/c.mkv", "3")


@pytest.mark.parametrize("item_id", ["42", "/library/metadata/42"])
def test_known_item_analyze_uses_normalized_id_and_put(mock_config, item_id):
    server = PlexServer(mock_config)
    plex = MagicMock()
    server._plex = plex

    assert server.refresh_preview_metadata("/media/video.mkv", item_id) is True

    assert _analyzed(plex) == ["42"]
    plex.library.sections.assert_not_called()


def test_invalid_item_id_does_not_issue_analyze(mock_config):
    server = PlexServer(mock_config)
    plex = MagicMock()
    server._plex = plex
    with pytest.raises(ValueError, match="numeric item ID"):
        server.refresh_preview_metadata("/media/video.mkv", "not-an-item")
    plex.query.assert_not_called()


@pytest.mark.parametrize("indexed", [False, True])
def test_hintless_notification_scans_then_analyzes_only_when_indexed(mock_config, indexed):
    mock_config.plex_url = "http://plex:32400"
    mock_config.plex_token = "test-token"
    mock_config.plex_verify_ssl = True
    mock_config.plex_library_ids = []
    mock_config.plex_libraries = []
    mock_config.path_mappings = []
    server = PlexServer(mock_config)
    plex = MagicMock()
    plex.library.sections.return_value = [SimpleNamespace(key="1", METADATA_TYPE="movie")]
    item = SimpleNamespace(ratingKey="42", media=[SimpleNamespace(parts=[SimpleNamespace(file="/media/video.mkv")])])
    plex.fetchItems.return_value = [item] if indexed else []
    server._plex = plex
    sections = MagicMock()
    sections.json.return_value = {
        "MediaContainer": {"Directory": [{"key": "1", "title": "Movies", "Location": [{"path": "/media"}]}]}
    }
    scanned = MagicMock(status_code=200)
    with patch("requests.get", side_effect=[sections, scanned]) as request:
        assert server.refresh_preview_metadata("/media/video.mkv") is indexed
    assert request.call_args_list == [
        call(
            "http://plex:32400/library/sections",
            headers={"X-Plex-Token": "test-token", "Accept": "application/json"},
            timeout=10,
            verify=True,
        ),
        call(
            "http://plex:32400/library/sections/1/refresh",
            params={"path": "/media"},
            headers={"X-Plex-Token": "test-token"},
            timeout=10,
            verify=True,
        ),
    ]
    plex.fetchItems.assert_called_once_with("/library/sections/1/all?type=1&file=video.mkv")
    if indexed:
        assert _analyzed(plex) == ["42"]
    else:
        plex.query.assert_not_called()


def _analyzed(plex) -> list[str]:
    """Item ids that got an Analyze PUT, in order."""
    return [
        c.args[0].split("/")[3]
        for c in plex.query.call_args_list
        if c.args[0].endswith("/analyze") and c.kwargs.get("method") is plex._session.put
    ]


def _join_worker(queue):
    worker = queue._worker
    if worker is not None:
        worker.join(2)
        assert not worker.is_alive(), "notification worker did not finish"


@pytest.mark.parametrize("outcome", [True, False, "offline"])
def test_a_waiting_chapter_publication_is_released_only_by_a_successful_analyze(outcome):
    def refresh(canonical_path, item_id):
        assert (canonical_path, item_id) == ("/media/video.mkv", "42")
        if outcome == "offline":
            raise ConnectionError("offline")
        return outcome

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(idle_seconds=0.01)
    try:
        assert queue.enqueue(server, "/media/video.mkv", "42")
        assert queue.wait("plex", "/media/video.mkv", required=True) is (outcome is True)
    finally:
        queue.close()


@pytest.mark.parametrize("required", [True, False])
def test_an_earlier_failed_analyze_only_holds_chapters_that_need_one(required):
    """A job that asked for no Analyze (Plex already shows the previews) mustn't wait on a failure from an earlier
    job: with nothing queued after it, that request can't replace chapters registered now."""
    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=MagicMock(return_value=False))
    queue = PlexRefreshQueue(idle_seconds=0.01)
    try:
        assert queue.enqueue(server, "/media/video.mkv", "42")
        assert queue.wait("plex", "/media/video.mkv", required=True) is False
        assert queue.wait("plex", "/media/video.mkv", required=required) is (not required)
    finally:
        queue.close()


def test_a_request_the_full_queue_turns_away_holds_chapters_that_need_an_analyze():
    """An earlier Analyze for the file doesn't stand in for the one a later job must send."""
    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=MagicMock(return_value=True))
    queue = PlexRefreshQueue(capacity=1, idle_seconds=0.01)
    started, release = Event(), Event()

    def busy(path, item_id):
        started.set()
        assert release.wait(2)
        return True

    try:
        assert queue.enqueue(server, "/media/video.mkv", "42")
        assert queue.wait("plex", "/media/video.mkv", required=True)
        other = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=busy)
        assert queue.enqueue(other, "/media/running.mkv", "1")
        assert started.wait(2)
        assert queue.enqueue(other, "/media/queued.mkv", "2")
        assert not queue.enqueue(server, "/media/video.mkv", "42")
        assert queue.wait("plex", "/media/video.mkv", required=True, timeout=0) is False
    finally:
        release.set()
        queue.close()


@pytest.mark.parametrize("item_id", [None, "/library/metadata/42"])
def test_an_analyze_sent_without_a_bare_item_id_is_remembered(item_id):
    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=MagicMock(return_value=True))
    queue = PlexRefreshQueue(idle_seconds=0.01)
    try:
        assert queue.enqueue(server, "/media/video.mkv", item_id)
        assert queue.wait("plex", "/media/video.mkv", required=True)
        assert queue.asked("plex", "/media/video.mkv", "42")
        assert queue.asked("plex", "/media/video.mkv", "43") is (item_id is None)
    finally:
        queue.close()


def test_a_file_whose_analyze_is_running_always_gets_its_follow_up():
    """Turned away, the newer request would be held, and the running one would then release that hold and report a
    completion the new preview never got."""
    started, release = Event(), Event()

    def refresh(path, item_id):
        started.set()
        assert release.wait(2)
        return True

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(capacity=1, idle_seconds=0.01)
    try:
        assert queue.enqueue(server, "/media/running.mkv", "1")
        assert started.wait(2)
        assert queue.enqueue(server, "/media/other.mkv", "2")
        assert not queue.enqueue(server, "/media/third.mkv", "3")
        assert queue.enqueue(server, "/media/running.mkv", "1")
        release.set()
        assert queue.wait("plex", "/media/running.mkv", required=True)
    finally:
        release.set()
        queue.close()


def test_a_held_file_never_queued_holds_chapters_that_need_an_analyze():
    """A held re-read lives only in the held list until the flush sends it, so a required wait fails closed."""
    queue = PlexRefreshQueue()
    try:
        assert queue.wait("plex", "/media/held.mkv", required=True, timeout=0) is False
    finally:
        queue.close()


def test_catch_up_requests_fill_at_most_half_the_queue():
    """A first scan after upgrade may find many unflagged files; newly written previews must still get a place."""
    started, release = Event(), Event()

    def refresh(path, item_id):
        started.set()
        assert release.wait(2)
        return True

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(capacity=4)
    try:
        assert queue.enqueue(server, "/media/running.mkv", "1")
        assert started.wait(2)
        assert queue.enqueue(server, "/media/q1.mkv", "2", quiet=True)
        assert queue.enqueue(server, "/media/q2.mkv", "3", quiet=True)
        assert not queue.enqueue(server, "/media/q3.mkv", "4", quiet=True)
        assert queue.enqueue(server, "/media/new1.mkv", "5")
        assert queue.enqueue(server, "/media/new2.mkv", "6")
        assert not queue.enqueue(server, "/media/new3.mkv", "7")
        # A new write for a file already queued as catch-up takes its place as a full request.
        assert queue.enqueue(server, "/media/q1.mkv", "2")
        assert queue._pending[("plex", "/media/q1.mkv")].quiet is False
        assert queue.enqueue(server, "/media/q3.mkv", "4", quiet=False) is False
    finally:
        release.set()
        queue.close()


@pytest.mark.parametrize("outcome", [True, False, "offline"])
def test_a_file_counts_as_asked_while_queued_and_after_plex_analyzed_it(outcome):
    started, release = Event(), Event()

    def refresh(path, item_id):
        started.set()
        assert release.wait(2)
        if outcome == "offline":
            raise ConnectionError("offline")
        return outcome

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=refresh)
    queue = PlexRefreshQueue(idle_seconds=0.01)
    try:
        assert not queue.asked("plex", "/media/video.mkv", "42")
        assert queue.enqueue(server, "/media/video.mkv", "42")
        assert started.wait(2)
        # While a request for the file is out, no item holding it is asked about again.
        assert queue.asked("plex", "/media/video.mkv", "42")
        assert queue.asked("plex", "/media/video.mkv", "43")
        release.set()
        queue.wait("plex", "/media/video.mkv")
        # Only an Analyze Plex ran is remembered, and only for that item: a failed request is asked again by a
        # later job, and another library's copy of the file still gets its own.
        assert queue.asked("plex", "/media/video.mkv", "42") is (outcome is True)
        assert not queue.asked("plex", "/media/video.mkv", "43")
        assert not queue.asked("other-plex", "/media/video.mkv", "42")
    finally:
        release.set()
        queue.close()


def _log_messages(level: str):
    """Capture loguru records at exactly ``level`` while the caller runs; returns (sink id, messages)."""
    from loguru import logger

    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]),
        level=level,
        filter=lambda record: record["level"].name == level,
    )
    return sink_id, messages


@pytest.mark.parametrize(("quiet", "level", "other"), [(False, "WARNING", "DEBUG"), (True, "DEBUG", "WARNING")])
def test_a_full_queue_logs_a_warning_unless_the_enqueue_is_quiet(quiet, level, other):
    from loguru import logger

    server = SimpleNamespace(id="plex", name="Plex", refresh_preview_metadata=MagicMock(return_value=True))
    queue = PlexRefreshQueue(capacity=0)
    expected_id, expected = _log_messages(level)
    unexpected_id, unexpected = _log_messages(other)
    try:
        assert not queue.enqueue(server, "/media/video.mkv", None, quiet=quiet)
    finally:
        logger.remove(expected_id)
        logger.remove(unexpected_id)
        queue.close()
    assert any("/media/video.mkv" in message for message in expected)
    assert not any("/media/video.mkv" in message for message in unexpected)
    server.refresh_preview_metadata.assert_not_called()


MULTI = "/tv/Show/Season 01/Show - S01E01-E02.mkv"


class _FakePlex:
    """Plex's item and season listings. Analyze flags the item's parts whose file has a preview, unless Plex
    'skips' that request; ``broken`` makes every read fail."""

    def __init__(self, items: dict[str, dict], *, skipped: dict[str, int] | None = None, broken: bool = False):
        self.items = items
        self.skipped = dict(skipped or {})
        self.broken = broken
        self._session = SimpleNamespace(put=object())
        self.query = MagicMock(side_effect=self._query)

    def _video(self, key: str) -> str:
        item = self.items[key]
        parts = "".join(f'<Part file="{file}"{FLAG if file in item["flagged"] else ""} />' for file in item["files"])
        parent = item.get("parent", "")
        return (
            f'<Video ratingKey="{key}" type="{item["type"]}" parentRatingKey="{parent}"><Media>{parts}</Media></Video>'
        )

    def _query(self, path: str, method=None):
        import xml.etree.ElementTree as ET

        key = path.split("/")[3]
        if path.endswith("/analyze"):
            if self.skipped.get(key, 0) > 0:
                self.skipped[key] -= 1
            else:
                item = self.items[key]
                item["flagged"] |= set(item.get("previews", item["files"]))
            return None
        if self.broken:
            raise ConnectionError("Plex is busy")
        if path.endswith("/children"):
            body = "".join(self._video(k) for k, item in self.items.items() if item.get("parent") == key)
        else:
            body = self._video(key)
        return ET.fromstring(f"<MediaContainer>{body}</MediaContainer>")


FLAG = ' indexes="sd"'


def _episode(*files, parent="7"):
    return {"type": "episode", "parent": parent, "files": list(files), "flagged": set()}


def _movie(*files, previews=None):
    return {"type": "movie", "files": list(files), "flagged": set(), "previews": previews or list(files)}


@pytest.fixture
def no_sleep():
    with patch("media_preview_generator.servers.plex.time.sleep") as sleep:
        yield sleep


class TestAnalyzeIsConfirmed:
    def _server(self, mock_config, fake):
        mock_config.path_mappings = []
        server = PlexServer(mock_config)
        server._plex = fake
        return server

    def test_every_dark_episode_sharing_the_file_is_analyzed(self, mock_config, no_sleep):
        fake = _FakePlex(
            {"41": _episode(MULTI), "42": _episode(MULTI), "43": _episode("/tv/Show/Season 01/Show - S01E03.mkv")}
        )

        assert self._server(mock_config, fake).refresh_preview_metadata(MULTI, "41") is True

        assert _analyzed(fake) == ["41", "42"]
        no_sleep.assert_not_called()

    @pytest.mark.parametrize("sibling", ["shown", "own-file"])
    def test_an_episode_already_showing_or_in_its_own_file_is_left_alone(self, mock_config, no_sleep, sibling):
        second = _episode(MULTI if sibling == "shown" else "/tv/Show/Season 01/Show - S01E02.mkv")
        if sibling == "shown":
            second["flagged"].add(MULTI)
        fake = _FakePlex({"41": _episode(MULTI), "42": second})

        assert self._server(mock_config, fake).refresh_preview_metadata(MULTI, "41") is True

        assert _analyzed(fake) == ["41"]

    def test_an_item_already_showing_is_not_analyzed_again(self, mock_config, no_sleep):
        movie = _movie("/m/Film/Film.mkv")
        movie["flagged"].add("/m/Film/Film.mkv")
        fake = _FakePlex({"5": movie})

        assert self._server(mock_config, fake).refresh_preview_metadata("/m/Film/Film.mkv", "5") is True

        assert _analyzed(fake) == []

    def test_a_showing_episode_is_skipped_but_its_dark_sibling_is_analyzed(self, mock_config, no_sleep):
        first = _episode(MULTI)
        first["flagged"].add(MULTI)
        fake = _FakePlex({"41": first, "42": _episode(MULTI)})

        assert self._server(mock_config, fake).refresh_preview_metadata(MULTI, "41") is True

        assert _analyzed(fake) == ["42"]

    def test_a_movie_never_lists_a_season(self, mock_config, no_sleep):
        fake = _FakePlex({"5": _movie("/m/Film/Film.mkv")})

        assert self._server(mock_config, fake).refresh_preview_metadata("/m/Film/Film.mkv", "5") is True

        assert _analyzed(fake) == ["5"]
        assert not [c for c in fake.query.call_args_list if c.args[0].endswith("/children")]

    def test_a_skipped_analyze_is_sent_again_for_the_dark_items_only(self, mock_config, no_sleep):
        fake = _FakePlex({"41": _episode(MULTI), "42": _episode(MULTI)}, skipped={"42": 1})

        assert self._server(mock_config, fake).refresh_preview_metadata(MULTI, "41") is True

        assert _analyzed(fake) == ["41", "42", "42"]
        no_sleep.assert_called_once()

    def test_still_dark_after_the_retry_still_counts_as_analyzed(self, mock_config, no_sleep):
        # Analyze finished either way, so chapters may register; Library health lists the file.
        fake = _FakePlex({"5": _movie("/m/Film/Film.mkv")}, skipped={"5": 9})

        assert self._server(mock_config, fake).refresh_preview_metadata("/m/Film/Film.mkv", "5") is True

        assert _analyzed(fake) == ["5", "5"]

    def test_another_version_without_a_preview_is_not_this_file(self, mock_config, no_sleep):
        hd, uhd = "/movies/Film (2000)/Film (2000).mkv", "/movies-4k/Film (2000)/Film (2000).mkv"
        fake = _FakePlex({"5": _movie(hd, uhd, previews=[hd])})

        assert self._server(mock_config, fake).refresh_preview_metadata(hd, "5") is True

        assert _analyzed(fake) == ["5"]
        no_sleep.assert_not_called()

    def test_unmapped_roots_match_on_folder_and_name(self, mock_config, no_sleep):
        fake = _FakePlex({"5": _movie("/plex/m/Film/Film.mkv")}, skipped={"5": 1})

        self._server(mock_config, fake).refresh_preview_metadata("/data/m/Film/Film.mkv", "5")

        assert _analyzed(fake) == ["5", "5"]

    @pytest.mark.parametrize("case", ["plex-unreadable", "file-not-on-item"])
    def test_when_plex_cannot_say_one_analyze_is_enough(self, mock_config, no_sleep, case):
        fake = _FakePlex({"5": _movie("/m/Other/Other.mkv")}, broken=case == "plex-unreadable")

        assert self._server(mock_config, fake).refresh_preview_metadata("/m/Film/Film.mkv", "5") is True

        assert _analyzed(fake) == ["5"]


class TestHeldWhileScheduledJobsWait:
    @pytest.fixture(autouse=True)
    def _config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path))

    def _run(self, monkeypatch, *, hold, refresh_result=True):
        from media_preview_generator.processing import plex_held_rereads, plex_refresh

        monkeypatch.setattr(plex_refresh, "should_hold", MagicMock(return_value=hold))
        monkeypatch.setattr(plex_refresh, "read_settings", MagicMock(return_value=None))
        server = SimpleNamespace(
            id="plex", name="Plex", refresh_preview_metadata=MagicMock(return_value=refresh_result)
        )
        return server, plex_held_rereads, plex_refresh

    @pytest.mark.parametrize("quiet", [False, True], ids=["new-previews", "catch-up"])
    def test_a_held_request_is_stored_and_plex_is_not_asked(self, monkeypatch, quiet):
        """A catch-up re-read starts Plex's scheduled jobs just like one for new previews, so it waits too."""
        server, held, refresh = self._run(monkeypatch, hold=True)
        path = "/media/a.mkv"
        queue = PlexRefreshQueue(idle_seconds=0.01)
        try:
            assert queue.enqueue(server, path, "42", library_id="3", quiet=quiet)
            assert queue.wait("plex", path, required=True) is False
            _join_worker(queue)
        finally:
            queue.close()

        refresh.should_hold.assert_called_once_with(server, "3")
        server.refresh_preview_metadata.assert_not_called()
        [entry] = held.entries("plex")
        assert (entry.canonical_path, entry.item_id, entry.library_id) == (path, "42", "3")
        assert not queue.asked("plex", path, "42")

    @pytest.mark.parametrize("outcome", [True, False, "offline"])
    def test_a_sent_request_releases_the_held_entry(self, monkeypatch, tmp_path, outcome):
        server, held, _refresh = self._run(monkeypatch, hold=False)
        if outcome == "offline":
            server.refresh_preview_metadata.side_effect = ConnectionError("offline")
        else:
            server.refresh_preview_metadata.return_value = outcome
        held.hold("plex", "/media/a.mkv", item_id="42", library_id="3")
        queue = PlexRefreshQueue(idle_seconds=0.01)
        try:
            assert queue.enqueue(server, "/media/a.mkv", "42", library_id="3")
            _join_worker(queue)
        finally:
            queue.close()

        server.refresh_preview_metadata.assert_called_once_with("/media/a.mkv", "42")
        assert held.entries("plex") == []

    def test_free_slots_counts_what_is_still_waiting(self):
        queue = PlexRefreshQueue(capacity=3)
        assert queue.free_slots() == 3
        queue.close()


def test_the_job_log_says_when_scheduled_jobs_make_plex_wait(monkeypatch):
    from loguru import logger

    from media_preview_generator.processing import multi_server
    from media_preview_generator.servers import plex_scheduled_jobs as jobs

    server = SimpleNamespace(id="plex-1", server_config=SimpleNamespace(output={}))
    settings = jobs.PlexJobSettings(
        server={"GenerateIntroMarkerBehavior": "scheduled", "LoudnessAnalysisBehavior": "scheduled"},
        libraries={},
        window_start=2,
        window_end=5,
    )
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        monkeypatch.setattr(multi_server, "cached_settings", lambda _id: settings)
        monkeypatch.setattr(multi_server, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 21, 12)))
        multi_server._log_held_refresh(server, "/media/Film/Film.mkv", "1")
        assert len(messages) == 1
        assert "Film.mkv: Plex runs intro markers and loudness analysis as a scheduled task" in messages[0]
        assert "(2:00–5:00)" in messages[0]

        messages.clear()
        monkeypatch.setattr(multi_server, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 21, 3)))
        multi_server._log_held_refresh(server, "/media/Film/Film.mkv", "1")
        monkeypatch.setattr(multi_server, "cached_settings", lambda _id: None)
        multi_server._log_held_refresh(server, "/media/Film/Film.mkv", "1")
        assert messages == []

        # Chapter thumbnails never hold, so the log must not say the preview waits.
        monkeypatch.setattr(multi_server, "cached_settings", lambda _id: settings)
        monkeypatch.setattr(multi_server, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 21, 12)))
        chapters = SimpleNamespace(id="plex-1", server_config=SimpleNamespace(output={"chapter_thumbnails": True}))
        multi_server._log_held_refresh(chapters, "/media/Film/Film.mkv", "1")
        assert messages == []
    finally:
        logger.remove(sink)


def test_is_queued_covers_pending_and_inflight_files():
    queue = PlexRefreshQueue()
    queue._pending[("plex", "/a")] = object()
    queue._inflight.add(("plex", "/b"))
    assert queue.is_queued("plex", "/a")
    assert queue.is_queued("plex", "/b")
    assert not queue.is_queued("plex", "/c")
    assert not queue.is_queued("other", "/a")
    queue._pending.clear()
    queue.close()
