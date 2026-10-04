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


@pytest.mark.parametrize("mode", ["never", "scheduled", "asap", None, "new-value", []])
def test_native_schedule_is_information_not_a_required_fix(mode):
    prefs = {"LoudnessAnalysisBehavior": mode}
    server = MagicMock()
    cfg = _cfg({"enabled": True})
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability", return_value=MagicMock(ready=True, message="")
    ) as capability:
        section = loudness_readiness_section(server, cfg, prefs)
    capability.assert_called_once_with(server, cfg)
    assert section["ok"] is True
    rows = {check["id"]: check for check in section["checks"]}
    if mode not in ("never", "scheduled", "asap"):
        assert "loudness_plex_analysis" not in rows
        return
    row = rows["loudness_plex_analysis"]
    assert row["ok"] is True and row["severity"] == "info" and row["informational"] is True
    assert "recommended" not in row and "fix_action" not in row
    assert row["bulk"] is False
    if mode == "never":
        assert row["actions"] == {}
    else:
        assert row["optional_action"] == "disable"
        assert row["optional_label"] == "Set to Never"
        action = row["actions"]["disable"]
        assert action["action"] == "set_plex_loudness_never"
        assert all(term in action["confirm"]["body"] for term in ["music", "unselected", "Existing measurements"])


@pytest.mark.parametrize(
    "library_ids,ready,action",
    [
        (None, True, True),
        (["1"], True, True),
        ([], True, False),
        (["missing"], True, False),
        (None, False, False),
        ([], False, False),
    ],
)
def test_optional_control_requires_current_writer_and_nonempty_video_selection(library_ids, ready, action):
    cfg = _cfg({"enabled": True, "library_ids": library_ids})
    server = MagicMock()
    with patch(
        "media_preview_generator.loudness.guard.loudness_capability",
        return_value=MagicMock(ready=ready, message="Writer not ready"),
    ) as capability:
        section = loudness_readiness_section(server, cfg, {"LoudnessAnalysisBehavior": "scheduled"})
    capability.assert_called_once_with(server, cfg)
    row = next(row for row in section["checks"] if row["id"] == "loudness_plex_analysis")
    assert section["ok"] is ready
    assert bool(row["actions"]) is action
    assert row["ok"] is True and row["informational"] is True
    if not action:
        assert row["reason"]


def test_native_information_remains_visible_when_probe_fails():
    with patch("media_preview_generator.loudness.guard.loudness_capability", side_effect=RuntimeError("offline")):
        section = loudness_readiness_section(
            MagicMock(), _cfg({"enabled": True}), {"LoudnessAnalysisBehavior": "scheduled"}
        )
    assert section["ok"] is False and section["severity"] == "critical"
    row = section["checks"][1]
    assert row["id"] == "loudness_plex_analysis" and row["actions"] == {}
    assert row["ok"] is True
    assert loudness_readiness_section(MagicMock(), _cfg({"enabled": False})) is None
