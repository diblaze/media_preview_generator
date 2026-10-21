"""Setup Health's fix for Plex's scheduled jobs: the readiness row and POST /api/servers/<id>/plex-scheduled-jobs."""

# The shared route harness exports pytest fixtures with these names.
# ruff: noqa: F811
from unittest.mock import patch

import pytest

from media_preview_generator.servers import plex_scheduled_jobs
from media_preview_generator.servers.plex import PlexServer
from media_preview_generator.servers.registry import server_config_from_dict

from .test_plex_detection_route import JELLYFIN, _plex, conn, seed  # noqa: F401 - fixtures

URL = "/api/servers/plex-1/plex-scheduled-jobs"
FETCH_PREFS = "media_preview_generator.servers.plex.PlexServer._fetch_prefs"


@pytest.fixture(autouse=True)
def _fresh_cache():
    plex_scheduled_jobs._cache.clear()
    yield
    plex_scheduled_jobs._cache.clear()


def _prefs(**values) -> list[dict]:
    return [{"id": pref, "value": value} for pref, value in values.items()]


def _server(markers_on: bool) -> PlexServer:
    return PlexServer(server_config_from_dict(_plex(enabled=markers_on)))


def test_needs_authentication(app, seed, conn):
    seed(_plex())
    assert app.test_client().post(URL).status_code == 401
    conn.query.assert_not_called()


def test_sends_exactly_the_recomputed_values_and_forgets_the_cached_settings(client, seed, conn):
    seed(_plex(enabled=True))
    plex_scheduled_jobs._cache["plex-1"] = (1e12, plex_scheduled_jobs.PlexJobSettings({}, {}, None, None))
    prefs = _prefs(
        GenerateIntroMarkerBehavior="scheduled",
        LoudnessAnalysisBehavior="scheduled",
        GenerateVADBehavior="asap",
    )
    with patch(FETCH_PREFS, return_value=prefs):
        resp = client.post(URL, json={"GenerateIntroMarkerBehavior": "asap", "evil": "1"})

    assert resp.get_json() == {"ok": True, "error": ""}
    # Intro & Credits is on for this server, so the app does intro itself; loudness is not its job here.
    conn.query.assert_called_once_with(
        "/:/prefs?GenerateIntroMarkerBehavior=never&LoudnessAnalysisBehavior=asap", method=conn._session.put
    )
    assert plex_scheduled_jobs.cached_settings("plex-1") is None


def test_nothing_to_change_is_refused_without_writing(client, seed, conn):
    seed(_plex(enabled=True))
    with patch(FETCH_PREFS, return_value=_prefs(GenerateIntroMarkerBehavior="never")):
        resp = client.post(URL)
    assert resp.status_code == 409
    conn.query.assert_not_called()


def test_plexs_refusal_is_reported_not_raised(client, seed, conn):
    seed(_plex(enabled=True))
    conn.query.side_effect = RuntimeError("(401) unauthorized")
    with patch(FETCH_PREFS, return_value=_prefs(LoudnessAnalysisBehavior="scheduled")):
        assert client.post(URL).get_json() == {"ok": False, "error": "(401) unauthorized"}


@pytest.mark.parametrize(
    ("server", "status"),
    [(JELLYFIN, 400), ({**_plex(), "enabled": False}, 409)],
    ids=["not-plex", "disabled-server"],
)
def test_refuses_a_server_the_row_could_not_show_for(client, seed, conn, server, status):
    seed(server)
    assert client.post(URL).status_code == status
    conn.query.assert_not_called()


def test_the_readiness_row_names_never_for_what_the_app_does_itself(conn):
    row = _server(markers_on=True)._hold_previews_check(
        dict(
            GenerateIntroMarkerBehavior="scheduled",
            LoudnessAnalysisBehavior="scheduled",
            ButlerStartHour=2,
            ButlerEndHour=5,
        )
    )
    assert row["ok"] is False
    assert row["current"] == "Intro markers, Loudness analysis: As a scheduled task"
    assert row["recommended"] == (
        "Intro markers: Never · Loudness analysis: As a scheduled task and when media is added"
    )
    assert row["actions"]["enable"]["action"] == "set_plex_jobs_for_previews"


def test_the_readiness_row_is_absent_when_no_job_waits_on_a_window(conn):
    assert _server(markers_on=True)._hold_previews_check(dict(GenerateIntroMarkerBehavior="asap")) is None
    assert _server(markers_on=True)._hold_previews_check({}) is None


def test_the_readiness_row_is_absent_with_chapter_thumbnails_on(conn):
    server = _server(markers_on=True)
    server.server_config.output["chapter_thumbnails"] = True
    prefs = dict(GenerateIntroMarkerBehavior="scheduled", LoudnessAnalysisBehavior="scheduled")
    assert server._hold_previews_check(prefs) is None
