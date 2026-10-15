"""Bell cards for nodes that need the user: one per node in use in an alert state, session-only dismissal."""

from __future__ import annotations

import pytest

from media_preview_generator.nodes import registry as reg
from media_preview_generator.web import notifications as notif
from media_preview_generator.web.routes.api_system import _is_known_notification_id


def _view(state, *, node_id="abc123", name="old-laptop", reason="It went quiet."):
    return reg.NodeView(
        id=node_id,
        name=name,
        url="http://secret-host:9494",
        enabled=True,
        state=state,
        reason=reason,
        version="1",
        roles=("worker",),
        gpus=(),
        cpu_cores=4,
        legacy=False,
        last_seen=None,
    )


class StubRegistry:
    def __init__(self, views):
        self._views = views

    def views(self):
        return list(self._views)


def _group_on(*node_ids):
    members = [{"id": f"m-{n}", "resource": "cpu", "device": None, "count": 1, "node": n} for n in node_ids]
    return [{"id": "g1", "name": "Nodes", "enabled": True, "members": members}]


def _plex_on(node_id, *, enabled):
    agent = {"enabled": enabled, "node_id": node_id}
    return [{"id": "p1", "type": "plex", "name": "Home", "markers": {"enabled": False, "plex": {"agent": agent}}}]


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

    notif.reset_session()
    reset_settings_manager()
    # The node the cards are about is in use: a worker group has a member on it.
    get_settings_manager(str(tmp_path)).apply_changes(updates={"worker_groups": _group_on("abc123")})
    yield
    notif.reset_session()
    reg.set_registry(None)
    reset_settings_manager()


def _cards(*views):
    reg.set_registry(StubRegistry(views))
    return notif._build_node_notifications()


@pytest.mark.parametrize(
    ("state", "name", "title"),
    [
        (reg.STATE_OFFLINE, "old-laptop", "old-laptop is offline"),
        (reg.STATE_UPDATE_NEEDED, "nuc", "nuc needs an update"),
        (reg.STATE_KEY_REFUSED, "nuc", "nuc refused the key"),
        (reg.STATE_ACCESS_PROBLEM, "nuc", "nuc can't see your media"),
        (reg.STATE_GPU_LOST, "storage", "storage lost its GPU"),
        (reg.STATE_NO_FFMPEG, "nuc", "nuc can't find FFmpeg"),
        (reg.STATE_DUPLICATE, "nuc", "nuc is added twice"),
    ],
)
def test_each_alert_state_gets_a_plain_card(state, name, title):
    (card,) = _cards(_view(state, name=name))
    assert card["id"] == f"node:abc123:{state}"
    assert card["title"] == title
    assert card["severity"] == "warning"
    assert (card["dismissable"], card["permanent_dismissable"], card["source"]) == (True, False, "nodes")
    assert card["action"]["href"] == "/nodes#node-abc123" and card["action"]["label"]
    assert "It went quiet." in card["body_html"]
    assert "secret-host" not in card["body_html"]


@pytest.mark.parametrize("state", [reg.STATE_READY, reg.STATE_CONNECTING, reg.STATE_DISABLED])
def test_healthy_connecting_and_turned_off_nodes_get_none(state):
    assert _cards(_view(state)) == []


def test_the_reason_and_name_are_escaped():
    (card,) = _cards(_view(reg.STATE_OFFLINE, name="<b>x</b>", reason="<script>"))
    assert "<script>" not in card["body_html"] and "&lt;script&gt;" in card["body_html"]


def test_dismissal_is_session_only_and_lifted_when_the_node_leaves_the_state():
    card_id = notif.node_notification_id("abc123", reg.STATE_OFFLINE)
    assert notif.is_session_only_dismissal(card_id)
    notif.dismiss_session(card_id)
    reg.set_registry(StubRegistry([_view(reg.STATE_OFFLINE)]))
    assert card_id not in [c["id"] for c in notif.build_active_notifications([])]
    # Back to ready, then offline again: the card shows again.
    _cards(_view(reg.STATE_READY))
    assert [c["id"] for c in _cards(_view(reg.STATE_OFFLINE))] == [card_id]
    assert not notif._session_is_dismissed(card_id)


def test_a_permanent_dismissal_never_hides_a_node_card():
    reg.set_registry(StubRegistry([_view(reg.STATE_OFFLINE)]))
    card_id = notif.node_notification_id("abc123", reg.STATE_OFFLINE)
    assert card_id in [c["id"] for c in notif.build_active_notifications([card_id])]


@pytest.mark.parametrize(
    ("notification_id", "known"),
    [
        ("node:abc123:offline", True),
        ("node:abc123:gpu_lost", True),
        ("node:abc123:ready", False),
        ("node:../../x:offline", False),
        ("node:abc123", False),
    ],
)
def test_the_dismiss_routes_know_node_card_ids(notification_id, known):
    assert _is_known_notification_id(notification_id) is known


@pytest.mark.parametrize(
    ("groups", "servers", "carded"),
    [
        ([], [], False),
        ([], _plex_on("abc123", enabled=False), False),
        ([], _plex_on("abc123", enabled=True), True),
        (_group_on("abc123"), [], True),
    ],
    ids=["nothing", "plex-database-node-switched-off", "plex-database-node-on", "group-member"],
)
def test_only_a_node_something_uses_gets_a_card(groups, servers, carded):
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().apply_changes(updates={"worker_groups": groups, "media_servers": servers})
    assert bool(_cards(_view(reg.STATE_OFFLINE))) is carded


def test_the_builder_survives_a_registry_that_fails():
    class Broken:
        def views(self):
            raise RuntimeError("boom")

    reg.set_registry(Broken())
    assert notif._build_node_notifications() == []
