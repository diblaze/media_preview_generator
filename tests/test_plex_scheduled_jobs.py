"""Plex's scheduled jobs: which ones an Analyze starts, Plex's maintenance window, and the Setup Health row."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.servers import plex_scheduled_jobs as jobs
from media_preview_generator.servers.plex_scheduled_jobs import PlexJobSettings


@pytest.fixture(autouse=True)
def _fresh_cache():
    jobs._cache.clear()
    yield
    jobs._cache.clear()


def _settings(server=None, libraries=None, start=2, end=5) -> PlexJobSettings:
    return PlexJobSettings(server=server or {}, libraries=libraries or {}, window_start=start, window_end=end)


def _keys(found) -> list[str]:
    return [job.key for job in found]


@pytest.mark.parametrize(
    ("mode", "library_switch", "expected"),
    [
        ("never", True, []),
        ("asap", True, []),
        ("scheduled", True, ["intro"]),
        ("scheduled", False, []),
        ("scheduled", None, ["intro"]),
    ],
    ids=["never", "asap", "scheduled-on", "scheduled-library-off", "scheduled-switch-missing"],
)
def test_intro_job_needs_scheduled_and_the_library_switch_on(mode, library_switch, expected):
    switches = {} if library_switch is None else {"enableIntroMarkerGeneration": library_switch}
    settings = _settings({"GenerateIntroMarkerBehavior": mode}, {"1": switches})
    assert _keys(jobs.held_jobs(settings, "1")) == expected


def test_unknown_or_missing_library_counts_as_on():
    settings = _settings(
        {"GenerateCreditsMarkerBehavior": "scheduled"}, {"1": {"enableCreditsMarkerGeneration": False}}
    )
    assert _keys(jobs.held_jobs(settings, "9")) == ["credits"]
    assert _keys(jobs.held_jobs(settings, None)) == ["credits"]
    assert jobs.held_jobs(settings, "1") == []


def test_chapter_thumbnails_have_no_library_switch():
    settings = _settings({"GenerateChapterThumbBehavior": "scheduled"}, {"1": {"enableIntroMarkerGeneration": False}})
    assert _keys(jobs.held_jobs(settings, "1")) == ["chapter_thumbs"]


def test_every_scheduled_job_is_listed_with_its_own_library_switch():
    server = {job.server_pref: "scheduled" for job in jobs.PLEX_JOBS}
    libraries = {"1": {"enableLoudnessAnalysis": False, "enableVoiceActivityGeneration": False}}
    assert _keys(jobs.held_jobs(_settings(server, libraries), "1")) == ["intro", "credits", "chapter_thumbs"]


@pytest.mark.parametrize(
    ("start", "end", "hour", "expected"),
    [
        (2, 5, 1, False),
        (2, 5, 2, True),
        (2, 5, 4, True),
        (2, 5, 5, False),
        (22, 3, 23, True),
        (22, 3, 2, True),
        (22, 3, 3, False),
        (22, 3, 12, False),
        (4, 4, 12, True),
        (None, None, 12, True),
        (2, None, 12, True),
    ],
)
def test_window_open(start, end, hour, expected):
    settings = _settings(start=start, end=end)
    assert jobs.window_open(settings, datetime(2026, 10, 21, hour, 30)) is expected


def test_window_text():
    assert jobs.window_text(_settings(start=2, end=5)) == "2:00–5:00"
    assert jobs.window_text(_settings(start=None, end=None)) == "unknown hours"


def _plex_server(prefs, sections, server_id="plex-1"):
    server = MagicMock()
    server.id = server_id
    server._fetch_prefs.return_value = [{"id": k, "value": v} for k, v in prefs.items()]
    server._connect.return_value.library.sections.return_value = sections
    return server


def _section(key, section_type="movie", **switches):
    settings = [SimpleNamespace(id=name, value=value) for name, value in switches.items()]
    return SimpleNamespace(key=key, type=section_type, settings=lambda: settings)


def test_read_settings_reads_prefs_and_library_switches_once_then_caches():
    server = _plex_server(
        {"GenerateIntroMarkerBehavior": "scheduled", "ButlerStartHour": 2, "ButlerEndHour": "5"},
        [_section("1", enableIntroMarkerGeneration="0"), _section("7", "artist")],
    )

    first = jobs.read_settings(server)
    second = jobs.read_settings(server)

    assert first is second
    assert first == PlexJobSettings(
        server={"GenerateIntroMarkerBehavior": "scheduled"},
        libraries={"1": {"enableIntroMarkerGeneration": False}},
        window_start=2,
        window_end=5,
    )
    server._fetch_prefs.assert_called_once()


def test_cache_expires_after_fifteen_minutes_and_invalidate_drops_it(monkeypatch):
    server = _plex_server({}, [])
    clock = [1000.0]
    monkeypatch.setattr(jobs.time, "monotonic", lambda: clock[0])
    jobs.read_settings(server)
    clock[0] += 899
    jobs.read_settings(server)
    assert server._fetch_prefs.call_count == 1
    clock[0] += 2
    jobs.read_settings(server)
    assert server._fetch_prefs.call_count == 2
    jobs.invalidate("plex-1")
    assert jobs.cached_settings("plex-1") is None
    jobs.read_settings(server)
    assert server._fetch_prefs.call_count == 3


def test_unreadable_plex_gives_none_and_is_not_cached():
    server = _plex_server({}, [])
    server._fetch_prefs.side_effect = ConnectionError("offline")
    assert jobs.read_settings(server) is None
    assert jobs.cached_settings("plex-1") is None
    assert jobs.should_hold(server, "1") is False


def test_fresh_read_replaces_the_cache():
    server = _plex_server({"ButlerStartHour": 1}, [])
    jobs.read_settings(server)
    server._fetch_prefs.return_value = [{"id": "ButlerStartHour", "value": 3}]
    assert jobs.read_settings(server).window_start == 1
    assert jobs.read_settings(server, fresh=True).window_start == 3
    assert jobs.read_settings(server).window_start == 3


def test_failed_fresh_read_keeps_the_older_cached_value():
    server = _plex_server({"ButlerStartHour": 1}, [])
    older = jobs.read_settings(server)
    server._fetch_prefs.side_effect = ConnectionError("offline")
    assert jobs.read_settings(server, fresh=True) is older
    assert jobs.cached_settings("plex-1") is older
    jobs.invalidate("plex-1")
    assert jobs.read_settings(server, fresh=True) is None


def _chapter_config(chapter_thumbnails):
    from media_preview_generator.servers.base import ServerConfig, ServerType

    return ServerConfig(
        id="s1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://x",
        auth={},
        libraries=[],
        output={"chapter_thumbnails": chapter_thumbnails},
    )


def test_chapter_thumbnails_never_hold():
    server = _plex_server({"LoudnessAnalysisBehavior": "scheduled", "ButlerStartHour": 2, "ButlerEndHour": 5}, [])
    noon = datetime(2026, 10, 21, 12)
    server.server_config = _chapter_config(True)
    assert jobs.should_hold(server, "1", noon) is False
    server.server_config = _chapter_config(False)
    assert jobs.should_hold(server, "1", noon) is True
    server.server_config = None
    assert jobs.should_hold(server, "1", noon) is True


def test_wanted_prefs_is_empty_with_chapter_thumbnails_on():
    server = _plex_server({"LoudnessAnalysisBehavior": "scheduled"}, [_section("1")])
    assert jobs.wanted_prefs(server, _chapter_config(True)) == {}


def test_should_hold_only_while_a_scheduled_job_waits_for_a_closed_window():
    server = _plex_server({"LoudnessAnalysisBehavior": "scheduled", "ButlerStartHour": 2, "ButlerEndHour": 5}, [])
    assert jobs.should_hold(server, "1", datetime(2026, 10, 21, 12)) is True
    assert jobs.should_hold(server, "1", datetime(2026, 10, 21, 3)) is False
    other = _plex_server({"LoudnessAnalysisBehavior": "asap", "ButlerStartHour": 2, "ButlerEndHour": 5}, [], "plex-2")
    assert jobs.should_hold(other, "1", datetime(2026, 10, 21, 12)) is False


def _readiness(prefs, library_prefs=None, libraries=("1",), done_by_app=(), can_write=True):
    return jobs.readiness_check(prefs, library_prefs or {}, list(libraries), set(done_by_app), can_write=can_write)


def test_readiness_has_no_row_without_scheduled_jobs():
    assert _readiness({"GenerateIntroMarkerBehavior": "never", "LoudnessAnalysisBehavior": "asap"}) is None


def test_readiness_ignores_a_job_switched_off_in_every_library_the_app_covers():
    prefs = {"GenerateIntroMarkerBehavior": "scheduled"}
    assert _readiness(prefs, {"1": {"enableIntroMarkerGeneration": False}, "2": {}}) is None
    assert _readiness(prefs, {"1": {"enableIntroMarkerGeneration": False}, "2": {}}, libraries=("1", "2")) is not None


def test_readiness_row_for_jobs_the_app_does_not_do():
    prefs = {
        "GenerateIntroMarkerBehavior": "scheduled",
        "LoudnessAnalysisBehavior": "scheduled",
        "ButlerStartHour": 2,
        "ButlerEndHour": 5,
    }
    check = _readiness(prefs)
    assert check["id"] == "plex_scheduled_jobs_hold_previews"
    assert check["ok"] is False
    assert check["severity"] == "recommended"
    assert check["label"] == "New previews only show in Plex after its maintenance window (2:00–5:00)"
    assert check["current"] == "Intro markers, Loudness analysis: As a scheduled task"
    assert check["recommended"] == "As a scheduled task and when media is added"
    assert check["fix_label"] == "Run them when media is added"
    assert check["fix_action"] == "enable"
    assert check["bulk"] is False
    assert check["actions"]["enable"]["action"] == "set_plex_jobs_for_previews"
    assert "Intro markers: As a scheduled task and when media is added" in check["actions"]["enable"]["confirm"]["body"]


def test_readiness_recommends_never_for_jobs_the_app_does_itself():
    prefs = {"GenerateIntroMarkerBehavior": "scheduled", "LoudnessAnalysisBehavior": "scheduled"}
    check = _readiness(prefs, done_by_app={"loudness"})
    assert (
        check["recommended"] == "Intro markers: As a scheduled task and when media is added · Loudness analysis: Never"
    )
    assert check["fix_label"] == "Apply these Plex settings"
    only_app = _readiness({"LoudnessAnalysisBehavior": "scheduled"}, done_by_app={"loudness"})
    assert only_app["recommended"] == "Never"


def test_readiness_offers_no_action_when_the_app_cannot_write():
    check = _readiness({"GenerateIntroMarkerBehavior": "scheduled"}, can_write=False)
    assert check["actions"] == {}
    assert "fix_action" not in check


def test_app_jobs_follow_what_the_app_does_for_the_server():
    from media_preview_generator.servers.base import Library, ServerConfig, ServerType

    def config(**kwargs):
        return ServerConfig(
            id="s1",
            type=ServerType.PLEX,
            name="Plex",
            enabled=True,
            url="http://x",
            auth={},
            libraries=[Library(id="1", name="Movies", remote_paths=(), enabled=True, kind="movie")],
            output={},
            **kwargs,
        )

    assert jobs.app_jobs(config()) == set()
    everything = config(
        markers={"enabled": True, "library_ids": None, "plex": {"db_write_confirmed_at": "2026-01-01"}},
        loudness={"enabled": True, "library_ids": None},
    )
    everything.output["chapter_thumbnails"] = True
    assert jobs.app_jobs(everything) == {"intro", "credits", "loudness", "chapter_thumbs"}
