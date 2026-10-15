"""A node's Setup Health envelope: sections, rows and must-fix / recommended buckets."""

from __future__ import annotations

import pytest

from media_preview_generator.markers.publishers.base import Capability, CapabilityReport
from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.protocol import ROLE_PLEX_DB, ROLE_WORKER
from media_preview_generator.nodes.readiness import node_readiness

GPU = {"type": "NVIDIA", "device": "cuda:0", "name": "RTX", "status": "ok"}


def _details(state=reg.STATE_READY, *, roles=(ROLE_WORKER,), gpus=(GPU,), legacy=False, access=None, reason=""):
    view = reg.NodeView(
        id="n1",
        name="nuc",
        url="http://nuc:9494",
        enabled=state != reg.STATE_DISABLED,
        state=state,
        reason=reason,
        version="9.9.9",
        roles=roles,
        gpus=gpus,
        cpu_cores=8,
        legacy=legacy,
        last_seen=1.0,
    )
    return reg.NodeDetails(
        view=view, platform={}, cpu={}, busy=0, info=None, access_result=access, access_checked_at=None
    )


def _envelope(details, *, groups=None, file_checks=None):
    return node_readiness(
        details,
        key="k",
        worker_groups=groups or [],
        app_version="9.9.9",
        file_checks=file_checks or (lambda url, key: pytest.fail("the Plex database must not be checked")),
    )


def _section(envelope, section_id):
    return next(s for s in envelope["sections"] if s["id"] == section_id)


def _check(envelope, check_id):
    return next(c for s in envelope["sections"] for c in s["checks"] if c["id"] == check_id)


def test_a_ready_worker_that_is_untested_and_unused_has_only_recommendations():
    envelope = _envelope(_details())
    assert envelope["overall_ok"] is True
    assert [s["id"] for s in envelope["sections"]] == ["node_connection", "node_media_access", "node_workers"]
    for section in envelope["sections"]:
        assert set(section) == {"id", "title", "docs_anchor", "ok", "severity", "checks"}
    assert _section(envelope, "node_connection")["ok"] is True
    untested = _check(envelope, "node_access_untested")
    assert (untested["ok"], untested["severity"], untested["label"]) == (False, "recommended", "Run Test access")
    assert (_check(envelope, "node_in_group")["ok"], _check(envelope, "node_in_group")["severity"]) == (
        False,
        "recommended",
    )
    assert _check(envelope, "node_gpu_cuda:0")["ok"] is True


@pytest.mark.parametrize(
    ("state", "failing"),
    [
        (reg.STATE_OFFLINE, "node_reachable"),
        (reg.STATE_KEY_REFUSED, "node_key"),
        (reg.STATE_UPDATE_NEEDED, "node_version"),
        (reg.STATE_NO_FFMPEG, "node_ffmpeg"),
    ],
)
def test_connection_problems_are_must_fix(state, failing):
    envelope = _envelope(_details(state, reason="why"))
    row = _check(envelope, failing)
    assert (row["ok"], row["severity"], row["reason"]) == (False, "critical", "why")
    assert envelope["overall_ok"] is False
    assert _section(envelope, "node_connection")["severity"] == "critical"


def test_a_worker_node_without_ffmpeg_says_so_and_the_fix_is_on_the_node():
    row = _check(_envelope(_details(reg.STATE_NO_FFMPEG, reason="nuc can't find FFmpeg")), "node_ffmpeg")
    assert (row["label"], row["fix_where"]) == ("FFmpeg isn't installed on this node", "node")
    assert _check(_envelope(_details()), "node_ffmpeg")["ok"] is True
    no_workers = _envelope(_details(roles=()))
    assert all(c["id"] != "node_ffmpeg" for s in no_workers["sections"] for c in s["checks"])


def test_an_offline_node_shows_only_its_connection():
    assert [s["id"] for s in _envelope(_details(reg.STATE_OFFLINE))["sections"]] == ["node_connection"]


def test_a_disabled_node_is_all_good_and_shows_nothing_else():
    envelope = _envelope(_details(reg.STATE_DISABLED))
    assert envelope["overall_ok"] is True
    assert [c["id"] for s in envelope["sections"] for c in s["checks"]] == ["node_disabled"]


def test_test_access_results_are_one_row_per_library_with_the_suggestion():
    access = {
        "libraries": [
            {"server": "Home", "library": "Movies", "app_root": "/data/movies", "checked": 3, "ok": True, "files": []},
            {
                "server": "Home",
                "library": "TV",
                "app_root": "/data/tv",
                "checked": 1,
                "ok": False,
                "files": [{"path": "/data/tv/a.mkv", "ok": False, "message": "Not found"}],
                "suggestion": {"from": "/mnt/tv", "to": "/mnt/TV"},
            },
        ]
    }
    envelope = _envelope(_details(access=access))
    ok, bad = _check(envelope, "node_access_0"), _check(envelope, "node_access_1")
    assert ok["ok"] is True and "Home · Movies" in ok["label"]
    assert (bad["ok"], bad["severity"], bad["reason"]) == (False, "critical", "Not found")
    assert (bad["current"], bad["recommended"]) == ("/mnt/tv", "/mnt/TV")
    assert (bad["fix_tab"], bad["fix_label"]) == ("media", "Use the suggested path")
    assert bad["suggestion"] == {"from": "/mnt/tv", "to": "/mnt/TV", "app_root": "/data/tv"}
    assert not {"fix_tab", "fix_label", "suggestion"} & set(ok)
    assert envelope["overall_ok"] is False


@pytest.mark.parametrize("error", ["This app can't open /data/kids", "No video files found near the top of /data/kids"])
def test_a_library_this_app_cant_sample_never_blames_the_node(error):
    """The lab's Synth libraries weren't mounted in the app: Setup Health said "the node can't see its files" (must
    fix, Open Media) although the node was never asked about them."""
    access = {
        "libraries": [
            {"server": "Home", "library": "Movies", "app_root": "/data/movies", "checked": 3, "ok": True, "files": []},
            {"server": "Home", "library": "Kids", "app_root": "/data/kids", "checked": 0, "ok": False, "files": [],
             "error": error, "suggestion": None},
        ]
    }  # fmt: skip
    envelope = _envelope(_details(access=access))
    row = _check(envelope, "node_access_1")
    assert (row["ok"], row["severity"], row["reason"]) == (False, "recommended", error)
    assert row["label"] == "Home · Kids: not tested, this app can't read it itself"
    assert (row["help_url"], row["help_label"]) == ("/servers", "Open Media Servers")
    assert not {"fix_tab", "fix_action", "suggestion"} & set(row)
    assert _section(envelope, "node_media_access")["severity"] == "recommended"
    assert envelope["overall_ok"] is True


@pytest.mark.parametrize(
    ("details", "check_id", "pointer"),
    [
        (_details(reg.STATE_OFFLINE), "node_reachable", {"fix_tab": "settings", "fix_label": "Check the address"}),
        (_details(reg.STATE_KEY_REFUSED), "node_key", {"fix_tab": "settings", "fix_label": "Change the key"}),
        (_details(reg.STATE_UPDATE_NEEDED), "node_version", {"fix_where": "node"}),
        (_details(reg.STATE_ACCESS_PROBLEM), "node_access_problem", {"fix_tab": "media", "fix_action": "test_access"}),
        (_details(), "node_access_untested", {"fix_tab": "media", "fix_action": "test_access"}),
        (_details(), "node_in_group", {"help_url": "/settings#section-workers", "help_label": "Open Workers"}),
        (_details(gpus=({**GPU, "status": "failed"},)), "node_gpu_cuda:0", {"fix_where": "node"}),
    ],
    ids=["offline", "key", "version", "access_problem", "untested", "group", "gpu"],
)
def test_every_failing_row_says_where_its_fix_is(details, check_id, pointer):
    row = _check(_envelope(details), check_id)
    assert row["ok"] is False
    assert {k: row.get(k) for k in pointer} == pointer


def test_passing_rows_carry_no_fix_pointer():
    groups = [{"id": "g", "members": [{"id": "m1", "node": "n1"}]}]
    envelope = _envelope(_details(), groups=groups)
    for check_id in ("node_reachable", "node_key", "node_version", "node_in_group", "node_gpu_cuda:0"):
        row = _check(envelope, check_id)
        assert row["ok"] is True
        assert not {"fix_tab", "fix_where", "fix_label", "help_url", "suggestion"} & set(row), check_id


def test_an_access_problem_is_must_fix():
    envelope = _envelope(_details(reg.STATE_ACCESS_PROBLEM, reason="3 files"))
    row = _check(envelope, "node_access_problem")
    assert (row["ok"], row["severity"], row["reason"]) == (False, "critical", "3 files")


def test_a_node_used_by_a_group_member_passes_and_a_failed_gpu_is_must_fix():
    groups = [{"id": "g", "members": [{"id": "m1", "node": "n1"}]}, {"id": "h", "members": [{"node": None}]}]
    envelope = _envelope(_details(gpus=({**GPU, "status": "failed"},)), groups=groups)
    assert _check(envelope, "node_in_group")["ok"] is True
    gpu = _check(envelope, "node_gpu_cuda:0")
    assert (gpu["ok"], gpu["severity"]) == (False, "critical")


def _report(state, message="", **details):
    return lambda url, key: CapabilityReport(state, message, details)


def test_a_plex_database_node_checks_the_database_on_its_machine():
    seen = []

    def checks(url, key):
        seen.append((url, key))
        return CapabilityReport(Capability.READY, "", {"db_path": "/plex/db", "fs_type": "ext4", "lock_holder": True})

    envelope = _envelope(_details(roles=(ROLE_PLEX_DB,), gpus=()), file_checks=checks)
    assert seen == [("http://nuc:9494", "k")]
    rows = _section(envelope, "node_plex_db")["checks"]
    assert [(r["id"], r["ok"]) for r in rows] == [
        ("plex_db_present", True),
        ("plex_db_local", True),
        ("plex_db_writable", True),
        ("plex_db_held", True),
    ]
    assert "node_media_access" not in [s["id"] for s in envelope["sections"]]


@pytest.mark.parametrize(
    ("report", "failing", "rows"),
    [
        (_report(Capability.AGENT_UNAVAILABLE, "can't reach"), "plex_db_reachable", 1),
        (_report(Capability.MISCONFIGURED, "not found"), "plex_db_present", 1),
        (_report(Capability.NEEDS_LOCAL_DB, "network share", db_path="/d", fs_type="nfs4"), "plex_db_local", 2),
        (_report(Capability.MISCONFIGURED, "can't write", db_path="/d", unwritable=["/d"]), "plex_db_writable", 3),
        (_report(Capability.UNREACHABLE, "Plex stopped", db_path="/d", lock_holder=False), "plex_db_held", 4),
    ],
    ids=["unavailable", "missing", "network", "unwritable", "not-held"],
)
def test_each_plex_database_problem_stops_at_its_own_row(report, failing, rows):
    envelope = _envelope(_details(roles=(ROLE_PLEX_DB,), gpus=()), file_checks=report)
    checks = _section(envelope, "node_plex_db")["checks"]
    assert len(checks) == rows
    assert (checks[-1]["id"], checks[-1]["ok"], checks[-1]["severity"]) == (failing, False, "critical")
    assert checks[-1]["fix_where"] == "node"
    assert envelope["overall_ok"] is False


def test_a_legacy_helper_is_recommended_an_update_and_gets_the_plex_database_rows():
    envelope = _envelope(
        _details(roles=(ROLE_PLEX_DB,), gpus=(), legacy=True),
        file_checks=_report(Capability.READY, "", db_path="/d", lock_holder=True),
    )
    legacy = _check(envelope, "node_legacy_helper")
    assert (legacy["ok"], legacy["severity"], legacy["fix_where"]) == (False, "recommended", "node")
    assert [s["id"] for s in envelope["sections"]] == ["node_connection", "node_plex_db"]
    assert envelope["overall_ok"] is True
