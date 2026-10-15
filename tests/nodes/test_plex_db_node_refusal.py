"""Nothing reaches a Plex database node that is turned off, or whose address answers as a different node.

Every path that resolves a server's Plex database node is covered: markers (``plex_database``), chapters
(``plex_chapters._context``), loudness (``create_loudness_db``) and Library Health (``library_health.plex._open_db``).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree.ElementTree import Element

import pytest

from media_preview_generator.library_health import plex as health_plex
from media_preview_generator.loudness import plex_db as loudness_db
from media_preview_generator.loudness.guard import RemoteLoudnessDb, create_loudness_db, loudness_capability
from media_preview_generator.markers.publishers import plex_chapters
from media_preview_generator.markers.publishers.base import Capability, PublishError
from media_preview_generator.markers.publishers.plex_remote import (
    AGENT_DIFFERENT_NODE,
    AGENT_DISABLED,
    AGENT_NO_NODE,
    AgentClient,
    AgentError,
    RemotePlexDb,
    plex_database,
)
from media_preview_generator.markers.settings import load_server
from media_preview_generator.nodes import registry as reg
from media_preview_generator.servers.base import ServerType
from tests.loudness.test_plex_db import FIELDS

NODE = {
    "id": "n1",
    "node_uid": "uid-1",
    "name": "Plex box",
    "url": "http://plex-host.lan:9494",
    "key": "node-key",
    "enabled": True,
    "path_mappings": [],
    "added_at": "2026-10-15T00:00:00+00:00",
}
MARKERS = {
    "enabled": True,
    "library_ids": None,
    "plex": {
        "db_write_confirmed_at": "2026-01-01",
        "on_plex_redetect": "restore",
        "agent": {"enabled": True, "node_id": "n1"},
    },
}


class StubRegistry:
    def __init__(self, different: set[str]):
        self.different = different

    def identity_ok(self, node_id: str) -> bool:
        return node_id not in self.different


@pytest.fixture
def node(tmp_path):
    """Store the node record; ``node(enabled=..., uid_ok=...)`` sets the one cell under test."""
    from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

    reset_settings_manager()
    sm = get_settings_manager(str(tmp_path))

    def configure(*, enabled: bool, uid_ok: bool) -> None:
        sm.set("nodes", [{**NODE, "enabled": enabled}])
        reg.set_registry(StubRegistry(set() if uid_ok else {"n1"}))

    yield configure
    reg.set_registry(None)
    reset_settings_manager()


# (enabled, uid_ok) -> the refusal's badge state, or None when the node is used.
CELLS = [
    pytest.param(True, True, None, id="enabled-uid-ok"),
    pytest.param(False, True, AGENT_DISABLED, id="disabled-uid-ok"),
    pytest.param(True, False, AGENT_DIFFERENT_NODE, id="enabled-uid-mismatch"),
    # Turned off wins: that is the one thing the user did, so it is what the message names.
    pytest.param(False, False, AGENT_DISABLED, id="disabled-uid-mismatch"),
]
MESSAGES = {
    AGENT_DISABLED: "Plex box is turned off on the Nodes page",
    AGENT_DIFFERENT_NODE: "A different node answered at Plex box's address",
}


def _cfg(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        id="p1",
        type=ServerType.PLEX,
        name="Home",
        markers=MARKERS,
        output={"plex_config_folder": str(tmp_path / "plex")},
        server_identity="plex-a",
    )


@pytest.mark.parametrize(("enabled", "uid_ok", "refused"), CELLS)
def test_markers(node, enabled, uid_ok, refused):
    node(enabled=enabled, uid_ok=uid_ok)
    session = MagicMock()
    db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home", session=session)
    assert isinstance(db, RemotePlexDb)
    if refused is None:
        assert db._client.refusal() is None
        return
    report = db.file_checks(deadline=1e12)
    assert report.state is Capability.AGENT_UNAVAILABLE
    assert report.message.startswith(MESSAGES[refused])
    assert report.details["agent"]["state"] == refused
    session.post.assert_not_called()
    session.get.assert_not_called()


@pytest.mark.parametrize(("enabled", "uid_ok", "refused"), CELLS)
def test_chapters(node, monkeypatch, enabled, uid_ok, refused):
    node(enabled=enabled, uid_ok=uid_ok)
    monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.43"))
    config = SimpleNamespace(markers=MARKERS, output={})
    if refused is None:
        backend, *_ = plex_chapters._context(MagicMock(), config)
        assert isinstance(backend, AgentClient) and backend.url == NODE["url"]
        return
    with pytest.raises(plex_chapters.ChapterError) as caught:
        plex_chapters._context(MagicMock(), config)
    assert str(caught.value).startswith(MESSAGES[refused])
    assert caught.value.state is Capability.AGENT_UNAVAILABLE


@pytest.mark.parametrize(("enabled", "uid_ok", "refused"), CELLS)
def test_loudness(node, tmp_path, monkeypatch, enabled, uid_ok, refused):
    node(enabled=enabled, uid_ok=uid_ok)
    sent = MagicMock(side_effect=AssertionError("nothing may be sent"))
    monkeypatch.setattr("requests.Session.post", sent)
    monkeypatch.setattr("requests.Session.get", sent)
    server = MagicMock()
    server._connect.return_value.query.return_value = Element(
        "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
    )
    cfg = _cfg(tmp_path)
    db = create_loudness_db(cfg, server)
    assert isinstance(db, RemoteLoudnessDb)
    if refused is None:
        assert db._remote._client.refusal() is None
        return
    report = loudness_capability(server, cfg)
    assert report.state is Capability.AGENT_UNAVAILABLE
    assert report.message.startswith(MESSAGES[refused])
    sent.assert_not_called()


@pytest.mark.parametrize(("enabled", "uid_ok", "refused"), CELLS)
def test_library_health(node, tmp_path, monkeypatch, enabled, uid_ok, refused):
    node(enabled=enabled, uid_ok=uid_ok)
    sent = MagicMock(side_effect=AssertionError("nothing may be sent"))
    monkeypatch.setattr("requests.Session.post", sent)
    monkeypatch.setattr("requests.Session.get", sent)
    db = health_plex._open_db(_cfg(tmp_path))
    assert isinstance(db, RemotePlexDb)
    if refused is None:
        assert db._client.refusal() is None
        return
    assert health_plex._node_refusal(db).startswith(MESSAGES[refused])
    sent.assert_not_called()


def test_a_node_record_that_is_gone_is_refused_with_a_message_naming_the_fix(node, tmp_path):
    from media_preview_generator.web.settings_manager import get_settings_manager

    node(enabled=True, uid_ok=True)
    get_settings_manager().set("nodes", [])
    db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home")
    report = db.file_checks(deadline=1e12)
    assert report.state is Capability.AGENT_UNAVAILABLE
    assert report.message == "This server has no Plex database node on the Nodes page. Choose one in Edit → Connection."
    assert report.details["agent"]["state"] == AGENT_NO_NODE


class TestLoudnessWithoutANode:
    """What the user sees when loudness is switched on through a node that can't be resolved."""

    def test_switched_on_without_a_node_chosen(self, node, tmp_path):
        node(enabled=True, uid_ok=True)
        cfg = _cfg(tmp_path)
        cfg.markers = {"plex": {"agent": {"enabled": True}}}
        report = loudness_capability(MagicMock(), cfg)
        assert report.state is Capability.MISCONFIGURED
        assert report.message == (
            "This server has no Plex database node on the Nodes page. Choose one in Edit → Connection."
        )

    def test_switched_on_with_a_node_that_was_removed(self, node, tmp_path):
        from media_preview_generator.web.settings_manager import get_settings_manager

        node(enabled=True, uid_ok=True)
        get_settings_manager().set("nodes", [])
        server = MagicMock()
        server._connect.return_value.query.return_value = Element(
            "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
        )
        report = loudness_capability(server, _cfg(tmp_path))
        assert report.state is Capability.AGENT_UNAVAILABLE
        assert report.message.startswith("This server has no Plex database node on the Nodes page.")


class TestTurnedOffAfterTheDatabaseWasOpened:
    """A job keeps its database for its whole run: turning the node off (or a different node turning up at its
    address) mid-job has to stop the very next request, not the next job's."""

    @pytest.fixture
    def nothing_sent(self, monkeypatch):
        sent = MagicMock(side_effect=AssertionError("nothing may be sent"))
        monkeypatch.setattr("requests.Session.post", sent)
        monkeypatch.setattr("requests.Session.get", sent)
        return sent

    TURNED = [
        pytest.param({"enabled": False, "uid_ok": True}, AGENT_DISABLED, id="turned-off"),
        pytest.param({"enabled": True, "uid_ok": False}, AGENT_DIFFERENT_NODE, id="different-node"),
    ]

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_markers(self, node, nothing_sent, change, refused):
        node(enabled=True, uid_ok=True)
        db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home")
        node(**change)
        report = db.file_checks(deadline=1e12)
        assert report.state is Capability.AGENT_UNAVAILABLE and report.message.startswith(MESSAGES[refused])
        assert report.details["agent"]["state"] == refused
        nothing_sent.assert_not_called()

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_chapters(self, node, nothing_sent, monkeypatch, change, refused):
        node(enabled=True, uid_ok=True)
        monkeypatch.setattr(plex_chapters, "_server_identity", lambda server, plex, reuse_recent: ("machine", "1.43"))
        backend, *_ = plex_chapters._context(MagicMock(), SimpleNamespace(markers=MARKERS, output={}))
        node(**change)
        with pytest.raises(AgentError) as caught:
            backend.post("/v1/chapters/write", {}, timeout=1)
        assert str(caught.value).startswith(MESSAGES[refused])
        nothing_sent.assert_not_called()

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_loudness(self, node, nothing_sent, tmp_path, change, refused):
        node(enabled=True, uid_ok=True)
        server = MagicMock()
        server._connect.return_value.query.return_value = Element(
            "MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123"
        )
        db = create_loudness_db(_cfg(tmp_path), server)
        node(**change)
        with pytest.raises(PublishError) as caught:
            loudness_db.write_stream(db, 11, FIELDS, deadline=time.monotonic() + 5)
        assert str(caught.value).startswith(MESSAGES[refused])
        assert caught.value.state is Capability.AGENT_UNAVAILABLE
        nothing_sent.assert_not_called()

    @pytest.mark.parametrize(("change", "refused"), TURNED)
    def test_library_health(self, node, nothing_sent, tmp_path, change, refused):
        node(enabled=True, uid_ok=True)
        db = health_plex._open_db(_cfg(tmp_path))
        node(**change)
        assert health_plex._node_refusal(db).startswith(MESSAGES[refused])
        nothing_sent.assert_not_called()

    def test_turned_back_on_it_is_used_again(self, node, nothing_sent):
        node(enabled=False, uid_ok=True)
        db = plex_database(load_server(MARKERS, "plex"), path_provider=lambda: "/local/db", label="Home")
        assert db.file_checks(deadline=1e12).details["agent"]["state"] == AGENT_DISABLED
        node(enabled=True, uid_ok=True)
        assert db._client.refusal() is None
