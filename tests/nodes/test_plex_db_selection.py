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
    with pytest.raises(plex_chapters.ChapterError, match="helper address and key"):
        _chapter_backend(monkeypatch, _markers({"enabled": True, "node_id": "gone"}))


def test_chapters_without_a_node_use_the_local_folder(monkeypatch, tmp_path):
    backend = _chapter_backend(
        monkeypatch, _markers({"enabled": False, "node_id": None}), {"plex_config_folder": str(tmp_path)}
    )
    assert not isinstance(backend, AgentClient)
