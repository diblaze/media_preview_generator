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
    """Earlier builds of v24 left the helper's own ``url``/``token`` beside ``node_id``: nothing reads them, and the
    next save drops them."""

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

    @pytest.mark.parametrize(
        "posted",
        [
            {"enabled": False, "node_id": None},
            {"enabled": False, "node_id": "n1", "url": "http://evil:1", "token": "****"},
        ],
        ids=["this-app", "posted-legacy-fields"],
    )
    def test_a_save_drops_the_retired_helper_key(self, posted):
        from media_preview_generator.markers.settings import validate_server

        block, err = validate_server(_markers(posted), "plex", _markers(dict(self.LEGACY)))
        assert err == ""
        assert block["plex"]["agent"] == {"enabled": False, "node_id": posted["node_id"]}
        assert "old-token" not in repr(block) and "evil" not in repr(block)

    def test_a_block_without_legacy_fields_gets_none(self):
        from media_preview_generator.markers.settings import validate_server

        block, _ = validate_server(_markers({"enabled": True, "node_id": "n1"}), "plex", None)
        assert block["plex"]["agent"] == {"enabled": True, "node_id": "n1"}

    def test_the_legacy_token_is_masked_for_the_browser(self):
        from media_preview_generator.markers.settings import mask_server

        masked = mask_server(_markers(dict(self.LEGACY)), "plex")
        assert masked["plex"]["agent"]["token"] == "****"
        assert "old-token" not in str(masked)


class TestChoosingANode:
    """``validate_server`` on a save that picks this server's Plex database node."""

    @pytest.fixture
    def roles(self, monkeypatch):
        """``roles(("worker",))`` makes the registry report those roles for n1 (None: it hasn't answered yet)."""
        from media_preview_generator.nodes import registry as reg

        reported: dict[str, object] = {"roles": None, "legacy": False}

        class StubRegistry:
            def view(self, node_id):
                if reported["roles"] is None:
                    return None
                return SimpleNamespace(name="Plex box", roles=reported["roles"], legacy=reported["legacy"])

        monkeypatch.setattr(reg, "get_registry", lambda: StubRegistry())

        def report(value, *, legacy=False):
            reported.update(roles=value, legacy=legacy)

        return report

    def _save(self, agent: dict, stored_agent: dict | None = None):
        from media_preview_generator.markers.settings import validate_server

        stored = _markers(stored_agent) if stored_agent is not None else None
        return validate_server(_markers(agent), "plex", stored)

    def test_a_node_without_the_plex_database_role_is_refused_with_the_fix(self, roles):
        roles(("worker",))
        block, err = self._save({"enabled": True, "node_id": "n1"}, {"enabled": False, "node_id": None})
        assert block is None
        assert err.startswith("Plex box doesn't have its Plex database role turned on")
        # The fix is the setting that turns the role on, not "turn that role on on the node".
        assert "NODE_ROLES=worker,plex_db" in err and " on on " not in err

    @pytest.mark.parametrize(
        ("reported", "legacy"),
        [(None, False), (("plex_db",), False), (("plex_db",), True)],
        ids=["unknown", "role", "old-helper"],
    )
    def test_a_node_with_the_role_or_not_heard_from_yet_is_accepted(self, roles, reported, legacy):
        roles(reported, legacy=legacy)
        block, err = self._save({"enabled": True, "node_id": "n1"}, {"enabled": False, "node_id": None})
        assert err == "" and block["plex"]["agent"] == {"enabled": True, "node_id": "n1"}

    def test_the_stored_choice_is_never_re_checked(self, roles):
        roles(("worker",))
        block, err = self._save({"enabled": True, "node_id": "n1"}, {"enabled": True, "node_id": "n1"})
        assert err == "" and block is not None

    def test_turning_it_on_without_a_node_is_refused(self, roles):
        block, err = self._save({"enabled": True, "node_id": None}, {"enabled": False, "node_id": None})
        assert block is None and "Choose the node" in err

    def test_a_stored_switched_on_block_without_a_node_is_kept_and_refused_no_node(self, roles):
        """A helper v24 couldn't move: it loads switched on with no node, so nothing opens this app's own copy."""
        block, err = self._save({"enabled": True, "node_id": None}, {"enabled": True, "node_id": None})
        assert err == "" and block["plex"]["agent"] == {"enabled": True, "node_id": None}
        settings = load_server(_markers({"enabled": True, "node_id": None}), "plex")
        db = plex_database(settings, path_provider=lambda: "/local/db", label="Plex")
        assert isinstance(db, RemotePlexDb) and db._client.refusal().agent_state == "no_node"

    def test_a_block_from_before_nodes_still_saves(self, roles):
        """What the Intro & Credits tab posts back for a server still holding the old helper shape."""
        from media_preview_generator.markers.settings import mask_server

        stored = {"enabled": True, "url": "http://helper.lan:9494", "token": "helper-key"}
        posted = mask_server(_markers(stored), "plex")["plex"]["agent"]
        block, err = self._save(posted, stored)
        assert err == "" and block["plex"]["agent"] == {"enabled": True, "node_id": None}
        assert "helper-key" not in repr(block)
