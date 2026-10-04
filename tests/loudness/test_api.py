"""The loudness save route (servers PUT) and POST /api/loudness/jobs."""

from __future__ import annotations

import pytest

from media_preview_generator.loudness import settings as ls
from tests.markers.conftest import api_headers

CONFIRMED = {"plex": {"db_write_confirmed_at": "2026-09-29T00:00:00+00:00"}}


@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"loudness_waiting": 2, "loudness_not_in_library": 1, "loudness_written": 4}, 3),
        ({"loudness_up_to_date": 2, "loudness_written": 1}, 0),
        ({"failed": 2}, 0),
    ],
)
def test_loudness_attempts_explain_pending_plex_and_retry_now_targets_its_child(client, counts, expected):
    from media_preview_generator.web.jobs import get_job_manager

    jm = get_job_manager()
    head = jm.create_job(kind="loudness", config={"is_retry_chain": True, "max_retries": 3})
    child = jm.create_job(
        kind="loudness",
        config={"is_retry": True, "parent_job_id": head.id, "retry_attempt": 1, "server_id": "plex-1"},
    )
    jm.set_publishers(
        child.id,
        [{"server_id": "plex-1", "server_name": "Home Plex", "server_type": "plex", "counts": counts}],
    )
    response = client.get(f"/api/jobs/{head.id}/attempts", headers=api_headers())
    assert response.status_code == 200
    attempt = next(row for row in response.get_json()["attempts"] if row["id"] == child.id)
    assert attempt["pending_servers"] == (
        [{"server_id": "plex-1", "server_name": "Home Plex", "server_type": "plex", "count": expected}]
        if expected
        else []
    )
    response = client.post(f"/api/jobs/{head.id}/retry-now", headers=api_headers())
    assert response.status_code == 200
    assert response.get_json()["retry_job_id"] == child.id
    assert child.config == {
        "is_retry": True,
        "parent_job_id": head.id,
        "retry_attempt": 1,
        "server_id": "plex-1",
        "force_fire_now": True,
    }


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


@pytest.mark.parametrize("enabled", ["false", "true", "", 0, 1, {}, [], None])
def test_save_rejects_non_boolean_enable_without_changing_the_opt_in(client, enabled):
    sid = _add_plex(loudness=ls.default_server_loudness())
    response = client.put(f"/api/servers/{sid}", json={"loudness": {"enabled": enabled}})
    assert response.status_code == 400
    assert response.get_json()["error"] == "loudness.enabled must be a boolean"
    assert _saved() == ls.default_server_loudness()


def test_save_enables_loudness_without_marker_write_confirmation(client):
    sid = _add_plex(markers={"enabled": False, "library_ids": None, "plex": {"db_write_confirmed_at": None}})
    resp = client.put(f"/api/servers/{sid}", json={"loudness": {"enabled": True}})
    assert resp.status_code == 200, resp.get_json()
    assert _saved()["enabled"] is True
    assert resp.get_json()["markers"]["enabled"] is False
    assert not resp.get_json()["markers"]["plex"].get("db_write_confirmed_at")


def test_save_refuses_loudness_with_unsupported_helper(client):
    sid = _add_plex(markers={"enabled": False, "plex": {"agent": {"enabled": True}}})
    resp = client.put(f"/api/servers/{sid}", json={"loudness": {"enabled": True}})
    assert resp.status_code == 400
    assert "helper does not support" in resp.get_json()["error"]


def test_enabling_helper_requires_turning_loudness_off(client):
    sid = _add_plex(loudness={"enabled": True})
    helper = {"plex": {"agent": {"enabled": True, "url": "http://helper:8765", "token": "test-helper-key-0123456789"}}}
    resp = client.put(f"/api/servers/{sid}", json={"markers": helper})
    assert resp.status_code == 400
    assert "helper does not support" in resp.get_json()["error"]
    resp = client.put(f"/api/servers/{sid}", json={"markers": helper, "loudness": {"enabled": False}})
    assert resp.status_code == 200, resp.get_json()
    assert _saved()["enabled"] is False


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


def test_save_refuses_explicit_music_library(client):
    sid = _add_plex(loudness=ls.default_server_loudness())
    libraries = [{"id": "music", "name": "Music", "kind": "track", "enabled": True, "remote_paths": []}]
    response = client.put(
        f"/api/servers/{sid}",
        json={
            "libraries": libraries,
            "loudness": {"enabled": True, "library_ids": ["music"]},
        },
    )
    assert response.status_code == 400
    assert "movie and TV" in response.get_json()["error"]
    assert not _saved()["enabled"]


@pytest.fixture
def created(monkeypatch):
    from media_preview_generator.loudness import job

    calls = []

    class _Job:
        def to_dict(self):
            return {"id": "x", "kind": "loudness"}

    monkeypatch.setattr(job, "create_loudness_job", lambda **kw: calls.append(kw) or _Job())
    return calls


def test_job_route_starts_a_library_job(client, created):
    body = {"libraries": [{"server_id": "plex-1", "library_id": 4}], "priority": "normal"}
    resp = client.post("/api/loudness/jobs", json=body, headers=api_headers())
    assert resp.status_code == 201, resp.get_json()
    assert created == [
        {
            "library_name": "Plex loudness: 1 library",
            "priority": 2,
            "source": "manual",
            "libraries": [{"server_id": "plex-1", "library_id": "4"}],
            "file_paths": [],
        }
    ]


def test_job_route_refuses_libraries_and_files_together(client, created):
    body = {"libraries": [{"server_id": "p", "library_id": "1"}], "file_paths": ["/x"]}
    resp = client.post("/api/loudness/jobs", json=body, headers=api_headers())
    assert resp.status_code == 400 and created == []
