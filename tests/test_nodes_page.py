"""Smoke test for the /nodes HTML page and its place in the navbar."""

from __future__ import annotations

import pytest

from media_preview_generator.web.settings_manager import get_settings_manager


@pytest.fixture
def mock_auth_config(tmp_path, monkeypatch):
    auth_file = str(tmp_path / "auth.json")
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    from media_preview_generator.web.settings_manager import reset_settings_manager

    reset_settings_manager()
    from media_preview_generator.web.routes import clear_gpu_cache

    clear_gpu_cache()
    return str(tmp_path)


@pytest.fixture
def flask_app(tmp_path, mock_auth_config):
    from media_preview_generator.web.app import create_app

    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


@pytest.fixture
def authenticated_client(flask_app):
    from media_preview_generator.web.auth import get_auth_token

    token = get_auth_token()
    get_settings_manager().set("setup_complete", True)
    client = flask_app.test_client()
    client.post("/login", data={"token": token}, follow_redirects=False)
    return client


class TestNodesPage:
    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/nodes", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers.get("Location", "")

    def test_authenticated_renders_the_page_and_its_dialogs(self, authenticated_client):
        response = authenticated_client.get("/nodes")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert "<title>Nodes · Media Preview Generator</title>" in body
        for landmark in (
            'id="nodeList"',
            'id="nodesCheckNow"',
            'id="nodesAddBtn"',
            'id="addNodeModal"',
            'id="addNodeUrl"',
            'placeholder="http://host.lan:9494"',
            'id="nodeModal"',
            'data-bs-target="#node-tab-health"',
            'data-bs-target="#node-tab-log"',
            "js/nodes.js",
            "css/pages/nodes.css",
        ):
            assert landmark in body, landmark
        # The key field never shows what was typed.
        assert 'type="password" class="form-control" id="addNodeKey"' in body

    def test_settings_menu_links_to_the_page_and_marks_it_current(self, authenticated_client):
        body = authenticated_client.get("/nodes").get_data(as_text=True)
        assert (
            'href="/nodes" aria-label="Nodes: other machines that lend this app their GPU, CPU or Plex database"'
            in (body.replace(' aria-current="page"', "").replace(" active", ""))
        )
        assert 'class="dropdown-item active" href="/nodes"' in body
        settings_toggle = body.split('id="navSettingsDropdown"')[0].rsplit("<a ", 1)[1]
        assert "active" in settings_toggle

    def test_other_pages_do_not_mark_nodes_current(self, authenticated_client):
        body = authenticated_client.get("/servers").get_data(as_text=True)
        assert 'href="/nodes"' in body
        assert 'class="dropdown-item active" href="/nodes"' not in body
