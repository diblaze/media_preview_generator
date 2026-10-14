"""A Plex server's database work goes to the node its ``agent.node_id`` names, with that node's address and key."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.publishers import plex_chapters
from media_preview_generator.markers.publishers.plex_db import LocalPlexDb
from media_preview_generator.markers.publishers.plex_remote import AgentClient, RemotePlexDb, plex_database
from media_preview_generator.markers.settings import load_server

NODE = {
    "id": "n1",
    "node_uid": None,
    "name": "Plex box",
    "url": "http://plex-host.lan:9494",
    "key": "node-key",
    "enabled": True,
    "path_mappings": [],
    "added_at": "2026-10-15T00:00:00+00:00",
}


@pytest.fixture(autouse=True)
def _nodes(tmp_path):
    from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

    reset_settings_manager()
    get_settings_manager(str(tmp_path)).set("nodes", [dict(NODE)])
    yield
    reset_settings_manager()


def _markers(agent: dict) -> dict:
    return {
        "enabled": True,
        "library_ids": None,
        "plex": {"db_write_confirmed_at": "2026-01-01", "on_plex_redetect": "restore", "agent": agent},
    }


def test_plex_database_uses_the_nodes_address_and_key():
    settings = load_server(_markers({"enabled": True, "node_id": "n1"}), "plex")
    db = plex_database(settings, path_provider=lambda: "/local/db", label="Plex")
    assert isinstance(db, RemotePlexDb)
    assert db._client.url == "http://plex-host.lan:9494"
    assert db._client._token == "node-key"


def test_a_switched_off_node_choice_writes_locally():
    settings = load_server(_markers({"enabled": False, "node_id": "n1"}), "plex")
    assert isinstance(plex_database(settings, path_provider=lambda: "/local/db", label="Plex"), LocalPlexDb)


def test_a_node_record_that_is_gone_never_falls_back_to_the_local_file():
    settings = load_server(_markers({"enabled": True, "node_id": "gone"}), "plex")
    db = plex_database(settings, path_provider=lambda: "/local/db", label="Plex")
    assert isinstance(db, RemotePlexDb) and db._client.url == ""


def test_a_change_to_the_node_record_is_what_the_next_load_sees():
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set("nodes", [{**NODE, "url": "http://moved:9494", "key": "rotated"}])
    settings = load_server(_markers({"enabled": True, "node_id": "n1"}), "plex")
    assert (settings.agent_url, settings.agent_token) == ("http://moved:9494", "rotated")


def _chapter_backend(monkeypatch, markers: dict, output: dict | None = None):
    monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.40"))
    server = MagicMock()
    config = SimpleNamespace(markers=markers, output=output or {})
    backend, *_ = plex_chapters._context(server, config)
    return backend


def test_chapters_go_to_the_nodes_address_and_key(monkeypatch):
    backend = _chapter_backend(monkeypatch, _markers({"enabled": True, "node_id": "n1"}))
    assert isinstance(backend, AgentClient)
    assert (backend.url, backend._token) == ("http://plex-host.lan:9494", "node-key")


def test_chapters_refuse_a_node_that_is_gone(monkeypatch):
    with pytest.raises(plex_chapters.ChapterError, match="no Plex database node on the Nodes page"):
        _chapter_backend(monkeypatch, _markers({"enabled": True, "node_id": "gone"}))


def test_chapters_without_a_node_use_the_local_folder(monkeypatch, tmp_path):
    backend = _chapter_backend(
        monkeypatch, _markers({"enabled": False, "node_id": None}), {"plex_config_folder": str(tmp_path)}
    )
    assert not isinstance(backend, AgentClient)


class TestLegacyHelperFields:
    """v24 leaves the helper's own ``url``/``token`` in the block for a rollback; nothing new reads or changes them."""

    LEGACY = {"enabled": True, "node_id": "n1", "url": "http://old-helper:9494", "token": "old-token"}

    def test_the_node_record_decides_the_address_not_the_legacy_fields(self):
        settings = load_server(_markers(dict(self.LEGACY)), "plex")
        assert (settings.agent_url, settings.agent_token) == ("http://plex-host.lan:9494", "node-key")
        db = plex_database(settings, path_provider=lambda: "/local/db", label="Plex")
        assert (db._client.url, db._client._token) == ("http://plex-host.lan:9494", "node-key")

    def test_a_gone_node_never_falls_back_to_the_legacy_address(self):
        settings = load_server(_markers({**self.LEGACY, "node_id": "gone"}), "plex")
        db = plex_database(settings, path_provider=lambda: "/local/db", label="Plex")
        assert isinstance(db, RemotePlexDb) and db._client.url == ""

    def test_a_save_keeps_the_stored_legacy_fields_and_ignores_posted_ones(self):
        from media_preview_generator.markers.settings import validate_server

        stored = _markers(dict(self.LEGACY))
        posted = _markers({"enabled": False, "node_id": "n1", "url": "http://evil:1", "token": "****"})
        block, err = validate_server(posted, "plex", stored)
        assert err == ""
        assert block["plex"]["agent"] == {
            "enabled": False,
            "node_id": "n1",
            "url": "http://old-helper:9494",
            "token": "old-token",
        }

    def test_a_block_without_legacy_fields_gets_none(self):
        from media_preview_generator.markers.settings import validate_server

        block, _ = validate_server(_markers({"enabled": True, "node_id": "n1"}), "plex", None)
        assert block["plex"]["agent"] == {"enabled": True, "node_id": "n1"}

    def test_the_legacy_token_is_masked_for_the_browser(self):
        from media_preview_generator.markers.settings import mask_server

        masked = mask_server(_markers(dict(self.LEGACY)), "plex")
        assert masked["plex"]["agent"]["token"] == "****"
        assert "old-token" not in str(masked)
