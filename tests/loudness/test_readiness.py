"""Loudness health reports unusable local writers independently of markers."""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.servers.loudness_readiness import loudness_readiness_section

from .test_settings import _cfg


@pytest.mark.parametrize("block", [{}, {"enabled": False}])
def test_disabled_loudness_never_probes_database(block):
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        assert loudness_readiness_section(MagicMock(), _cfg(block)) is None
    capability.assert_not_called()


@pytest.mark.parametrize("enabled", ["false", "true", "", 0, 1, {}, [], None])
def test_invalid_enable_is_reported_without_probing_the_database(enabled):
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": enabled}))
    capability.assert_not_called()
    assert not section["ok"]
    assert section["checks"][0]["reason"] == "loudness.enabled must be a boolean"


@pytest.mark.parametrize("ready", [True, False])
def test_health_reports_capability_without_marker_consent(ready):
    config = _cfg({"enabled": True}, markers={"enabled": False})
    server = MagicMock()
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability",
        return_value=MagicMock(ready=ready, message="Wrong server"),
    ) as capability:
        section = loudness_readiness_section(server, config)
    capability.assert_called_once_with(server, config)
    assert section["ok"] is ready
    check = section["checks"][0]
    assert check["reason"] == (None if ready else "Wrong server")
    assert check["severity"] == ("recommended" if ready else "critical")
    assert check["actions"] == {}


def test_failed_probe_is_unhealthy_with_actionable_message():
    with patch("media_preview_generator.loudness.guard.loudness_capability", side_effect=RuntimeError("offline")):
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True}))
    assert not section["ok"]
    assert "Check the Plex connection" in section["checks"][0]["reason"]


def test_stored_unsupported_music_selection_is_not_reported_as_ready():
    with patch("media_preview_generator.loudness.guard.loudness_capability") as capability:
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True, "library_ids": ["3"]}))
    capability.assert_not_called()
    assert not section["ok"]
    assert "movie and TV" in section["checks"][0]["reason"]


@pytest.mark.parametrize(
    ("mode", "row", "ok"),
    [
        ("never", "Plex's own loudness analysis is off", True),
        ("scheduled", "Plex also analyses loudness itself", False),
        ("asap", "Plex also analyses loudness itself", False),
        (None, None, True),
        ("new-value", None, True),
        ([], None, True),
    ],
)
def test_plexs_own_loudness_analysis_gets_a_row_when_its_setting_is_known(mode, row, ok):
    prefs = {} if mode is None else {"LoudnessAnalysisBehavior": mode}
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability", return_value=MagicMock(ready=True, message="")
    ):
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True}), prefs)
    rows = {check["id"]: check for check in section["checks"]}
    assert section["ok"] is ok
    if row is None:
        assert "loudness_plex_analysis" not in rows
        return
    check = rows["loudness_plex_analysis"]
    assert (check["label"], check["ok"], check["recommended"], check["severity"]) == (row, ok, "Never", "recommended")
    assert (check["reason"] is None) is ok
    if ok:
        assert check["actions"] == {}
    else:
        action = check["actions"][check["fix_action"]]
        assert (action["action"], check["fix_label"], check["bulk"]) == (
            "set_plex_loudness_never",
            "Set to Never",
            False,
        )
        assert "music" in action["confirm"]["body"]


def test_plexs_setting_is_left_out_while_loudness_cant_be_stored_or_is_off():
    prefs = {"LoudnessAnalysisBehavior": "scheduled"}
    with patch("media_preview_generator.loudness.guard.loudness_capability", side_effect=RuntimeError("offline")):
        section = loudness_readiness_section(MagicMock(), _cfg({"enabled": True}), prefs)
    assert [c["id"] for c in section["checks"]] == ["loudness_registration"] and section["severity"] == "critical"
    assert loudness_readiness_section(MagicMock(), _cfg({"enabled": False}), prefs) is None
