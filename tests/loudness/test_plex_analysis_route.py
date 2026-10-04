"""Setup Health's Set to Never for Plex's own loudness analysis: POST /api/servers/<id>/plex-loudness-analysis."""

from __future__ import annotations

import pytest

from ..markers.test_plex_detection_route import JELLYFIN, _plex, conn, seed  # noqa: F401 - fixtures

URL = "/api/servers/plex-1/plex-loudness-analysis"


def _loudness_plex(enabled: bool = True) -> dict:
    return {**_plex(enabled=False), "loudness": {"enabled": enabled, "library_ids": None}}


def test_needs_authentication(app, seed, conn):  # noqa: F811
    seed(_loudness_plex())
    assert app.test_client().post(URL).status_code == 401
    conn.query.assert_not_called()


@pytest.mark.parametrize("library_ids", [None, []], ids=["default-libraries", "none-chosen-yet"])
def test_sets_only_the_loudness_pref_to_never(client, seed, conn, library_ids):  # noqa: F811
    seed({**_loudness_plex(), "loudness": {"enabled": True, "library_ids": library_ids}})
    resp = client.post(URL)
    assert resp.get_json() == {"ok": True, "error": ""}
    conn.query.assert_called_once_with("/:/prefs?LoudnessAnalysisBehavior=never", method=conn._session.put)


@pytest.mark.parametrize(
    ("server", "url", "status", "error"),
    [
        (JELLYFIN, URL, 400, "Plex's marker settings are Plex settings"),
        ({**_loudness_plex(), "enabled": False}, URL, 409, "is disabled"),
        (_loudness_plex(enabled=False), URL, 400, "Loudness is off for this server"),
        (_loudness_plex(), "/api/servers/nope/plex-loudness-analysis", 404, "not found"),
    ],
    ids=["not-plex", "disabled-server", "loudness-off", "unknown-server"],
)
def test_refuses_a_server_the_row_could_not_show_for(client, seed, conn, server, url, status, error):  # noqa: F811
    seed(server)
    resp = client.post(url)
    assert resp.status_code == status and error in resp.get_json()["error"]
    conn.query.assert_not_called()


def test_plexs_refusal_is_reported_not_raised(client, seed, conn):  # noqa: F811
    seed(_loudness_plex())
    conn.query.side_effect = RuntimeError("(401) unauthorized")
    assert client.post(URL).get_json() == {"ok": False, "error": "(401) unauthorized"}
