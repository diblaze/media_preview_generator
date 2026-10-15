"""Bell cards for nodes that need the user: one per node in use in an alert state, session-only dismissal."""

from __future__ import annotations

import dataclasses

import pytest
from markupsafe import escape

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


def _plex_on(node_id, *, enabled, server_enabled=None):
    agent = {"enabled": enabled, "node_id": node_id}
    server = {"id": "p1", "type": "plex", "name": "Home", "markers": {"enabled": False, "plex": {"agent": agent}}}
    if server_enabled is not None:
        server["enabled"] = server_enabled
    return [server]


def _group_off(*node_ids):
    return [{**group, "enabled": False} for group in _group_on(*node_ids)]


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

    # The bell also builds cards from this machine's own state (its GPUs, Vulkan, mounts, time zone): on CI, with no
    # GPU, some of those fire and add cards these tests don't count. Only the node cards are under test here.
    node_cards = notif._build_node_notifications
    monkeypatch.setattr(notif, "_notification_sources", lambda: list(node_cards()))

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
    ("state", "name", "title", "href"),
    [
        (reg.STATE_OFFLINE, "old-laptop", "old-laptop is offline", "/nodes#node-abc123"),
        (reg.STATE_UPDATE_NEEDED, "nuc", "nuc needs an update", "/nodes#node-abc123/health"),
        (reg.STATE_KEY_REFUSED, "nuc", "nuc refused the key", "/nodes#node-abc123/settings"),
        (reg.STATE_ACCESS_PROBLEM, "nuc", "nuc can't see your media", "/nodes#node-abc123/media/test"),
        (reg.STATE_GPU_LOST, "storage", "storage lost its GPU", "/nodes#node-abc123/health"),
        (reg.STATE_NO_FFMPEG, "nuc", "nuc can't find FFmpeg", "/nodes#node-abc123/health"),
        # Settings holds the Remove button; Overview had nothing to do there.
        (reg.STATE_DUPLICATE, "nuc", "nuc is added twice", "/nodes#node-abc123/settings"),
        (reg.STATE_TEMP_PROBLEM, "nuc", "nuc can't use its temp folder", "/nodes#node-abc123/health"),
    ],
)
def test_each_alert_state_gets_a_plain_card_whose_action_opens_the_tab_with_the_fix(state, name, title, href):
    (card,) = _cards(_view(state, name=name))
    assert card["id"] == f"node:abc123:{state}"
    assert card["title"] == title
    assert card["severity"] == "warning"
    assert (card["dismissable"], card["permanent_dismissable"], card["source"]) == (True, False, "nodes")
    assert card["action"]["href"] == href and card["action"]["label"]
    assert "It went quiet." in card["body_html"]
    assert "secret-host" not in card["body_html"]


# What the registry says for each state: every one already ends with what to do.
REGISTRY_REASONS = {
    reg.STATE_OFFLINE: ("flaky-node didn't answer. Check that it is running and reachable from this app.", "running"),
    reg.STATE_UPDATE_NEEDED: (
        "old-node runs version 4.4.2 and this app runs 4.4.3. Update the node to 4.4.3.",
        "update the node",
    ),
    reg.STATE_KEY_REFUSED: (
        "flaky-node refused the key. Copy the key from the node's log (Docker) or node.json (Windows) into this node's key field.",
        "copy",
    ),
    reg.STATE_ACCESS_PROBLEM: (
        "flaky-node couldn't see 3 different files in 10 minutes (last: not found on the node). Its workers are "
        "paused: run Test access and fix the folder mappings.",
        "test access",
    ),
    reg.STATE_GPU_LOST: (
        "flaky-node lost its GPU cuda:0 (not found). Its CPU workers keep running. Check the GPU driver on the node.",
        "cpu workers keep running",
    ),
}


@pytest.mark.parametrize("state", sorted(REGISTRY_REASONS))
def test_the_card_says_what_to_do_once(state):
    reason, instruction = REGISTRY_REASONS[state]
    (card,) = _cards(_view(state, reason=reason))
    assert card["body_html"].lower().count(instruction) == 1, card["body_html"]


def test_a_restarting_node_is_called_restarting_not_offline():
    view = dataclasses.replace(
        _view(reg.STATE_OFFLINE, reason="old-laptop is restarting; it gets no files until it is back."),
        cause=reg.OFFLINE_RESTARTING,
    )
    (card,) = _cards(view)
    assert card["title"] == "old-laptop is restarting"
    assert card["id"] == "node:abc123:restarting"
    (other,) = _cards(dataclasses.replace(view, cause=reg.OFFLINE_UNREACHABLE))
    assert other["title"] == "old-laptop is offline"
    assert other["id"] == "node:abc123:offline"


def test_a_node_that_asks_this_app_to_wait_is_not_called_offline():
    """Its own reason, right under the title, says it answered and only asks this app to wait."""
    reason = (
        "rl asks this app to wait: too many wrong keys came from this app's address. Something else there (another "
        "app, an old copy) has an old key: give it the right one, or stop it."
    )
    view = dataclasses.replace(_view(reg.STATE_OFFLINE, name="rl", reason=reason), cause=reg.OFFLINE_RATE_LIMITED)
    (card,) = _cards(view)
    assert card["title"] == "rl asks this app to wait"
    assert card["id"] == "node:abc123:rate_limited"


def _offline(cause, *, reason="old-laptop didn't answer. Check that it is running and reachable."):
    return dataclasses.replace(_view(reg.STATE_OFFLINE, reason=reason), cause=cause)


@pytest.mark.parametrize(
    ("answered", "title"),
    [
        (reg.OFFLINE_RESTARTING, "old-laptop is restarting"),
        (reg.OFFLINE_RATE_LIMITED, "old-laptop asks this app to wait"),
    ],
)
@pytest.mark.parametrize("then", [reg.OFFLINE_UNREACHABLE, reg.OFFLINE_STUCK, reg.OFFLINE_NOT_A_NODE])
def test_a_dismissed_card_for_a_node_that_answered_does_not_hide_it_going_offline(answered, title, then):
    """Lab: a node whose image update never started again. Its "is restarting" card was dismissed (it was expected
    back), the restart window ran out, and the bell stayed quiet: all three cards shared the id node:<id>:offline."""
    reg.set_registry(StubRegistry([_offline(answered)]))
    (card,) = notif.build_active_notifications([])
    assert card["title"] == title
    notif.dismiss_session(card["id"])
    assert notif.build_active_notifications([]) == []

    reg.set_registry(StubRegistry([_offline(then)]))
    assert [c["title"] for c in notif.build_active_notifications([])] == ["old-laptop is offline"]

    # It answers again with the same problem: the dismissal of the card it left is lifted.
    reg.set_registry(StubRegistry([_offline(answered)]))
    assert [c["title"] for c in notif.build_active_notifications([])] == [title]


@pytest.mark.parametrize("cause", [reg.OFFLINE_RESTARTING, reg.OFFLINE_RATE_LIMITED, reg.OFFLINE_UNREACHABLE])
def test_a_dismissed_node_card_stays_dismissed_while_the_node_stays_that_way(cause):
    reg.set_registry(StubRegistry([_offline(cause)]))
    (card,) = notif.build_active_notifications([])
    notif.dismiss_session(card["id"])
    for _ in range(3):
        assert notif.build_active_notifications([]) == []


def test_an_offline_node_that_stops_answering_another_way_keeps_its_dismissed_offline_card_hidden():
    """Both read "<name> is offline": the user already said they know."""
    reg.set_registry(StubRegistry([_offline(reg.OFFLINE_UNREACHABLE)]))
    (card,) = notif.build_active_notifications([])
    notif.dismiss_session(card["id"])
    reg.set_registry(StubRegistry([_offline(reg.OFFLINE_STUCK, reason="old-laptop accepts connections but ...")]))
    assert notif.build_active_notifications([]) == []


def test_the_node_card_kinds_are_the_alert_states_and_the_causes_with_a_card_of_their_own():
    assert set(notif.NODE_CARD_KINDS) == {
        *reg.ALERT_STATES,
        reg.OFFLINE_RESTARTING,
        reg.OFFLINE_RATE_LIMITED,
        reg.UPDATE_APP_BEHIND,
    }


def _update_needed(cause, reason):
    view = _view(reg.STATE_UPDATE_NEEDED, name="nuc", reason=reason)
    return dataclasses.replace(view, version="9.9.10", cause=cause)


def test_the_bell_says_this_app_needs_the_update_when_the_node_is_ahead():
    """Its reason and Setup Health said "Update this app", while the bell's title said "nuc needs an update"."""
    reason = "nuc runs version 9.9.10 and this app runs 9.9.9. Update this app to 9.9.10."
    (card,) = _cards(_update_needed(reg.UPDATE_APP_BEHIND, reason))
    assert card["title"] == "Update this app for nuc"
    assert card["id"] == "node:abc123:app_behind"
    assert escape(reason) in card["body_html"]
    # Setup Health's version row is where "This app needs an update" is explained.
    assert card["action"] == {"href": "/nodes#node-abc123/health", "label": "Open Nodes"}


def test_the_bell_says_the_node_needs_the_update_when_it_is_behind():
    reason = "nuc runs version 9.9.8 and this app runs 9.9.9. Update the node to 9.9.9."
    (card,) = _cards(_update_needed("", reason))
    assert (card["title"], card["id"]) == ("nuc needs an update", "node:abc123:update_needed")


def test_a_cause_only_gives_its_own_card_in_its_own_state():
    (card,) = _cards(dataclasses.replace(_view(reg.STATE_OFFLINE), cause=reg.UPDATE_APP_BEHIND))
    assert (card["title"], card["id"]) == ("old-laptop is offline", "node:abc123:offline")


def test_a_node_without_a_reason_still_gets_what_to_do():
    (card,) = _cards(_view(reg.STATE_OFFLINE, reason=""))
    assert "Check that the node is running" in card["body_html"]


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
        ("node:abc123:restarting", True),
        ("node:abc123:rate_limited", True),
        ("node:abc123:app_behind", True),
        ("node:abc123:gpu_lost", True),
        ("node:abc123:unconfirmed", True),
        ("node:abc123:ready", False),
        ("node:abc123:stuck", False),
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
        ([], _plex_on("abc123", enabled=True, server_enabled=True), True),
        ([], _plex_on("abc123", enabled=True, server_enabled=False), False),
        (_group_on("abc123"), [], True),
        (_group_off("abc123"), [], False),
    ],
    ids=[
        "nothing",
        "plex-database-node-switched-off",
        "plex-database-node-on",
        "plex-database-node-on-server-on",
        "plex-database-node-on-server-switched-off",
        "group-member",
        "member-of-a-switched-off-group",
    ],
)
def test_only_a_node_something_switched_on_uses_gets_a_card(groups, servers, carded):
    """A switched-off server or group runs nothing, so a node only it uses keeps no offline card up (5.1.0 never
    contacted a switched-off server's helper)."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().apply_changes(updates={"worker_groups": groups, "media_servers": servers})
    assert bool(_cards(_view(reg.STATE_OFFLINE))) is carded


def _unconfirmed(state=reg.STATE_READY):
    return dataclasses.replace(_view(state, name="plex-box", reason=""), roles=("plex_db",), plex_db_unconfirmed=True)


def test_a_plex_database_node_with_a_new_id_gets_a_card_that_opens_its_key():
    """Until now only the Servers page said it; the node's card showed Ready and the bell stayed quiet."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().apply_changes(
        updates={"worker_groups": [], "media_servers": _plex_on("abc123", enabled=True)}
    )

    (card,) = _cards(_unconfirmed())

    assert card["id"] == "node:abc123:unconfirmed"
    assert card["title"] == "plex-box reports a new id"
    assert "Type the node's key again on its Settings tab" in card["body_html"]
    assert card["action"] == {"href": "/nodes#node-abc123/settings", "label": "Type the key again"}
    assert notif.is_session_only_dismissal(card["id"])


@pytest.mark.parametrize("saved_uid", ["uid-1", None], ids=["rolled-back", "never-gave-an-id"])
def test_a_plex_database_node_rolled_back_to_an_older_release_gets_a_card(saved_uid):
    """Lab: a node rolled back to the released helper had every Plex database request refused and markers jobs
    skipping every file, while its card read Ready and the bell stayed quiet. One that never gave an id keeps working."""
    from media_preview_generator.nodes.client import NodeProtocolError
    from media_preview_generator.web.settings_manager import get_settings_manager
    from tests.nodes.test_registry import Harness, _record

    get_settings_manager().apply_changes(updates={"worker_groups": [], "media_servers": _plex_on("n1", enabled=True)})
    h = Harness(nodes=[_record(node_uid=saved_uid)])
    try:
        h.client().default_info = NodeProtocolError("404")  # an older Plex helper doesn't know /v1/info
        h.registry.check_now("n1")
        reg.set_registry(h.registry)
        cards = notif._build_node_notifications()
    finally:
        h.registry.stop()

    if saved_uid is None:
        assert cards == []
        return
    (card,) = cards
    assert card["id"] == "node:n1:update_needed"
    assert card["title"] == "nuc needs an update"
    assert card["body_html"] == f"<p class='mb-0'>{escape(reg.no_id_reason('nuc'))}</p>"
    assert card["action"] == {"href": "/nodes#node-n1/health", "label": "Open Nodes"}


def test_a_new_id_raises_no_card_when_no_switched_on_server_uses_the_node():
    """A worker group member doesn't send Plex database work, so it doesn't make the new id matter."""
    assert _cards(_unconfirmed()) == []


def test_a_new_id_card_comes_back_after_the_key_is_confirmed_and_it_happens_again():
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().apply_changes(
        updates={"worker_groups": [], "media_servers": _plex_on("abc123", enabled=True)}
    )
    card_id = notif.node_notification_id("abc123", notif.NODE_UNCONFIRMED)
    notif.dismiss_session(card_id)

    _cards(dataclasses.replace(_unconfirmed(), plex_db_unconfirmed=False))

    assert not notif._session_is_dismissed(card_id)


def test_a_switched_off_group_or_server_still_counts_as_using_the_node_for_used_by_and_remove():
    from media_preview_generator.nodes.settings import node_users

    groups, servers = _group_off("abc123"), _plex_on("abc123", enabled=True, server_enabled=False)

    assert node_users("abc123", groups, servers) == {
        "groups": [{"id": "g1", "name": "Nodes"}],
        "servers": [{"id": "p1", "name": "Home"}],
    }
    assert node_users("abc123", groups, servers, switched_on_only=True) == {"groups": [], "servers": []}


def test_the_builder_survives_a_registry_that_fails():
    class Broken:
        def views(self):
            raise RuntimeError("boom")

    reg.set_registry(Broken())
    assert notif._build_node_notifications() == []
