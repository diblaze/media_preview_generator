"""The loudness save route (servers PUT) and POST /api/loudness/jobs."""

from __future__ import annotations

from media_preview_generator.loudness import settings as ls

CONFIRMED = {"plex": {"db_write_confirmed_at": "2026-09-29T00:00:00+00:00"}}


def _add_plex(markers=None, loudness=None):
    from media_preview_generator.web.settings_manager import get_settings_manager

    entry = {
        "id": "plex-1",
        "type": "plex",
        "name": "plex",
        "enabled": True,
        "url": "http://x:1",
        "auth": {},
        "libraries": [],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {"plex_config_folder": "/tmp"},
        "markers": markers or {"enabled": False, "library_ids": None, **CONFIRMED},
    }
    if loudness is not None:
        entry["loudness"] = loudness
    get_settings_manager().set("media_servers", [entry])
    return entry["id"]


def _saved():
    from media_preview_generator.web.settings_manager import get_settings_manager

    return get_settings_manager().get("media_servers")[0]["loudness"]


def test_save_enables_loudness_and_keeps_the_library_choice(client):
    sid = _add_plex(loudness={"enabled": False, "library_ids": ["4"]})
    resp = client.put(f"/api/servers/{sid}", json={"loudness": {"enabled": True}})
    assert resp.status_code == 200, resp.get_json()
    assert _saved() == {"enabled": True, "library_ids": ["4"]}
    assert resp.get_json()["loudness"] == {"enabled": True, "library_ids": ["4"]}


def test_save_refuses_loudness_before_the_database_write_is_confirmed(client):
    sid = _add_plex(markers={"enabled": False, "library_ids": None, "plex": {"db_write_confirmed_at": None}})
    resp = client.put(f"/api/servers/{sid}", json={"loudness": {"enabled": True}})
    assert resp.status_code == 400 and "Confirm" in resp.get_json()["error"]


def test_save_without_loudness_key_carries_the_block_forward(client):
    sid = _add_plex(loudness={"enabled": True, "library_ids": ["2"]})
    assert client.put(f"/api/servers/{sid}", json={"name": "renamed"}).status_code == 200
    assert _saved() == {"enabled": True, "library_ids": ["2"]}


def test_non_plex_servers_keep_no_loudness_block(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set(
        "media_servers",
        [{"id": "jf-1", "type": "jellyfin", "name": "jf", "enabled": True, "url": "http://x:1", "auth": {}}],
    )
    assert client.put("/api/servers/jf-1", json={"name": "renamed", "loudness": {}}).status_code == 200
    assert "loudness" not in get_settings_manager().get("media_servers")[0]


def test_save_gives_a_server_without_a_block_the_default(client):
    sid = _add_plex()
    assert client.put(f"/api/servers/{sid}", json={"name": "renamed"}).status_code == 200
    assert _saved() == ls.default_server_loudness()
