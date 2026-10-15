"""An unreadable settings.json at start-up is left untouched and never opens setup."""

import json

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

BAD_FILES = {
    "cut_short": b'{"setup_complete": true, "plex_url": "http://plex:32400", "servers": [{"id": "a", ',
    "empty": b"",
    "invalid_json": b"not json at all {{{",
    "non_object": b'["a", "list"]',
}


def _make_app(tmp_path):
    from media_preview_generator.web.app import create_app

    reset_settings_manager()
    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture(autouse=True)
def _isolated_auth(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_AUTH_TOKEN", raising=False)
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", str(tmp_path / "auth.json"))
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: str(tmp_path))
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    yield
    reset_settings_manager()


def _assert_sign_in_required(client):
    body = {"token": "attacker-token-1234", "confirm_token": "attacker-token-1234"}
    resp = client.post("/api/setup/set-token", data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 401
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/jobs").status_code == 401


@pytest.mark.parametrize("name", list(BAD_FILES))
def test_unreadable_settings_untouched_and_sign_in_required(tmp_path, name):
    settings_file = tmp_path / "settings.json"
    settings_file.write_bytes(BAD_FILES[name])

    app = _make_app(tmp_path)
    sm = get_settings_manager()
    assert sm.load_failed
    assert sm.is_setup_complete()
    assert settings_file.read_bytes() == BAD_FILES[name]

    from media_preview_generator.web.scheduler import _quiet_hours_recompute_and_apply

    sm.set("quiet_hours", {"enabled": True})
    _quiet_hours_recompute_and_apply(drain=False)
    assert settings_file.read_bytes() == BAD_FILES[name]
    assert not list(tmp_path.glob("settings.json.*"))
    _assert_sign_in_required(app.test_client())

    restarted = _make_app(tmp_path)
    assert settings_file.read_bytes() == BAD_FILES[name]
    _assert_sign_in_required(restarted.test_client())


def test_healthy_start_still_migrates_and_saves(tmp_path):
    _make_app(tmp_path)
    sm = get_settings_manager()
    assert not sm.load_failed
    assert json.loads((tmp_path / "settings.json").read_text())["_schema_version"] >= 1
    sm.set("log_level", "DEBUG")
    assert json.loads((tmp_path / "settings.json").read_text())["log_level"] == "DEBUG"
