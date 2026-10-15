"""A node's Setup Health envelope: sections, rows and must-fix / recommended buckets."""

from __future__ import annotations

import dataclasses

import pytest

from media_preview_generator.markers.publishers.base import Capability, CapabilityReport
from media_preview_generator.nodes import registry as reg
from media_preview_generator.nodes.protocol import ROLE_PLEX_DB, ROLE_WORKER
from media_preview_generator.nodes.readiness import UPDATE_DOCS_URL, node_readiness, to_fix

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
        (reg.STATE_TEMP_PROBLEM, "node_temp"),
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


def test_a_worker_node_that_cant_use_its_temp_folder_says_so_and_the_fix_is_on_the_node():
    row = _check(_envelope(_details(reg.STATE_TEMP_PROBLEM, reason="nuc's temp folder can't be used")), "node_temp")
    assert (row["label"], row["fix_where"], row["reason"]) == (
        "The node can't use its temp folder",
        "node",
        "nuc's temp folder can't be used",
    )
    assert _check(_envelope(_details()), "node_temp")["ok"] is True
    # No FFmpeg is checked first: the temp folder isn't known then, so no row claims it is fine.
    no_ffmpeg = _envelope(_details(reg.STATE_NO_FFMPEG))
    assert all(c["id"] != "node_temp" for s in no_ffmpeg["sections"] for c in s["checks"])


def test_an_offline_node_shows_only_its_connection():
    assert [s["id"] for s in _envelope(_details(reg.STATE_OFFLINE))["sections"]] == ["node_connection"]


def test_a_disabled_node_is_one_neutral_note_and_nothing_else():
    envelope = _envelope(_details(reg.STATE_DISABLED))
    assert envelope["overall_ok"] is True
    assert [c["id"] for s in envelope["sections"] for c in s["checks"]] == ["node_disabled"]
    row = _check(envelope, "node_disabled")
    assert (row["label"], row["severity"], row["ok"], row["informational"]) == (
        "Turned off — no work is sent",
        "info",
        False,
        True,
    )


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


def test_no_explanation_carries_library_data():
    """An explanation is shown as HTML; a library folder named like markup must only reach escaped fields."""
    evil = "/media/<img src=x onerror=alert(1)>"
    library = {"server": evil, "library": evil, "app_root": evil, "error": evil, "files": []}
    access = {
        "libraries": [
            {**library, "checked": 3, "ok": True},
            {**library, "checked": 1, "ok": False, "suggestion": {"from": evil, "to": evil}},
            {**library, "checked": 0, "ok": False},
            {**library, "checked": 0, "ok": False, "empty": True},
        ]
    }
    envelope = _envelope(_details(access=access))
    rows = [c for s in envelope["sections"] for c in s["checks"]]
    assert [r["explanation"] for r in rows if "<" in r["explanation"]] == []
    assert evil in _check(envelope, "node_access_0")["tooltip"]


@pytest.mark.parametrize(
    "error", ["This app can't open /data/kids", "Timed out reading /data/kids (no answer within 20 s)"]
)
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
    row = _check(envelope, "node_access_skipped")
    assert (row["ok"], row["severity"], row["informational"], row["reason"]) == (False, "info", True, "Home · Kids")
    assert row["label"] == "1 library skipped: this app can't open it"
    assert (row["help_url"], row["help_label"]) == ("/servers", "Fix on Media Servers")
    assert not {"fix_tab", "fix_action", "suggestion"} & set(row)
    assert _section(envelope, "node_media_access")["severity"] == "recommended"
    assert envelope["overall_ok"] is True


def test_libraries_this_app_cant_open_share_one_neutral_note():
    """Seven Synth libraries the app had no mount for made seven Recommended rows on every node, drowning the one
    library the node really couldn't see."""
    skipped = [
        {"server": "Home", "library": f"Synth {i}", "app_root": f"/media/synth{i}", "checked": 0, "ok": False,
         "files": [], "error": f"This app can't open /media/synth{i}", "suggestion": None}
        for i in range(7)
    ]  # fmt: skip
    envelope = _envelope(_details(access={"libraries": skipped}))
    rows = _section(envelope, "node_media_access")["checks"]
    notes = [r for r in rows if r["id"].startswith("node_access_")]
    assert [r["id"] for r in notes] == ["node_access_skipped"]
    assert notes[0]["label"] == "7 libraries skipped: this app can't open them"
    assert not [r for r in rows if r["ok"] is False and r["severity"] in ("critical", "recommended")]


def test_an_empty_library_is_not_a_problem():
    access = {
        "libraries": [
            {"server": "Home", "library": "Kids", "app_root": "/data/kids", "checked": 0, "ok": False, "files": [],
             "error": "No video files found near the top of /data/kids", "empty": True, "suggestion": None},
        ]
    }  # fmt: skip
    envelope = _envelope(_details(access=access))
    row = _check(envelope, "node_access_0")
    assert (row["ok"], row["severity"]) == (True, "recommended")
    assert row["label"] == "Home · Kids: no video files in this library yet"
    assert not {"fix_tab", "fix_action", "help_url", "suggestion"} & set(row)
    assert _section(envelope, "node_media_access")["ok"] is True


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


UNWRITABLE = {"db_path": "/plex/db", "fs_type": "ext4", "unwritable": ["/plex/db"], "writer_uid": "1000"}


@pytest.mark.parametrize(
    ("report", "row_id", "says"),
    [
        (
            _report(
                Capability.MISCONFIGURED,
                "This app (user 1000) can't write /plex/db. Run it with the user that owns Plex's database (user 998).",
                **UNWRITABLE,
                db_owner_uid="998",
            ),
            "plex_db_writable",
            "The Plex database node (user 1000) can't write /plex/db. Run the node as the user that owns Plex's "
            "database (user 998).",
        ),
        (
            _report(
                Capability.MISCONFIGURED,
                "This app (user 1000) can't write /plex/db. Run it with the user that owns Plex's database (user 1000).",
                **UNWRITABLE,
                db_owner_uid="1000",
            ),
            "plex_db_writable",
            "The Plex database node (user 1000) can't write /plex/db, though it runs as the user that owns Plex's "
            "database: the folder is read-only.",
        ),
        (
            _report(
                Capability.NEEDS_LOCAL_DB,
                "Plex's database is on a network share (nfs4). The app must run on the same machine as Plex to write "
                "markers; Plex stays read-only.",
                db_path="/plex/db",
                fs_type="nfs4",
            ),
            "plex_db_local",
            "Plex's database is on a network share (nfs4) where the Plex database node runs.",
        ),
        (
            _report(
                Capability.UNREACHABLE,
                "Plex doesn't have its database open through this folder right now (Plex is stopped, or this app "
                "sees a different path to the file).",
                db_path="/plex/db",
                fs_type="ext4",
                lock_holder=False,
            ),
            "plex_db_held",
            "the node sees a different path to the file",
        ),
    ],
    ids=["another-user", "read-only-folder", "network-share", "not-held-open"],
)
def test_a_plex_database_refusal_talks_about_the_node_not_this_app(report, row_id, says):
    """The check ran on the node: "This app ..." would send the user to change the app the node exists to leave be."""
    envelope = _envelope(_details(roles=(ROLE_PLEX_DB,), gpus=()), file_checks=report)
    row = next(r for r in _section(envelope, "node_plex_db")["checks"] if r["id"] == row_id)
    assert row["ok"] is False
    assert says in row["reason"]
    assert "this app" not in row["reason"].lower() and "The app must" not in row["reason"]


def test_a_plex_database_nodes_version_row_shows_its_plex_database_version():
    """The row explains the Plex database role's minimum, so it shows that role's version, not the package's."""
    details = _details(roles=(ROLE_PLEX_DB,), gpus=())
    info = {
        "node": {"id": "x", "name": "plex-box", "version": "0.0.0.dev0", "build": "", "roles": [ROLE_PLEX_DB]},
        "agent": {"version": "1.2.0", "protocols": [1], "capabilities": ["chapters_v1", "loudness_v1"]},
    }
    envelope = _envelope(
        dataclasses.replace(details, view=dataclasses.replace(details.view, version="0.0.0.dev0"), info=info),
        file_checks=_report(Capability.READY, "", db_path="/d", fs_type="ext4", lock_holder=True),
    )
    row = next(c for s in envelope["sections"] for c in s["checks"] if c["id"] == "node_version")
    assert "of its Plex database role" in row["explanation"]
    assert row["current"] == "1.2.0"


def test_a_legacy_helper_is_recommended_an_update_and_gets_the_plex_database_rows():
    envelope = _envelope(
        _details(roles=(ROLE_PLEX_DB,), gpus=(), legacy=True),
        file_checks=_report(Capability.READY, "", db_path="/d", lock_holder=True),
    )
    legacy = _check(envelope, "node_legacy_helper")
    assert (legacy["ok"], legacy["severity"], legacy["fix_where"]) == (False, "recommended", "node")
    assert [s["id"] for s in envelope["sections"]] == ["node_connection", "node_plex_db"]
    assert envelope["overall_ok"] is True


@pytest.mark.parametrize("legacy", [False, True], ids=["plex-db-node", "old-plex-helper"])
def test_a_plex_database_nodes_version_row_says_new_enough_not_matching(legacy):
    envelope = _envelope(
        _details(roles=(ROLE_PLEX_DB,), gpus=(), legacy=legacy),
        file_checks=_report(Capability.READY, "", db_path="/d", lock_holder=True),
    )
    row = _check(envelope, "node_version")
    assert (row["ok"], row["label"]) == (True, "The node's version is new enough")
    assert "1.0.0 or newer" in row["explanation"]


def test_a_worker_on_another_build_of_the_same_version_shows_both_short_commits():
    details = _details(reg.STATE_UPDATE_NEEDED, reason="runs another build")
    details = dataclasses.replace(details, info={"node": {"build": "b" * 40}})
    envelope = node_readiness(
        details,
        key="k",
        worker_groups=[],
        app_version="9.9.9",
        app_build="a" * 40,
        file_checks=lambda url, key: pytest.fail("the Plex database must not be checked"),
    )
    row = _check(envelope, "node_version")
    assert (row["ok"], row["current"], row["recommended"]) == (False, "9.9.9 (bbbbbbb)", "9.9.9 (aaaaaaa)")


def test_a_worker_on_another_version_shows_the_versions_only():
    details = _details(reg.STATE_UPDATE_NEEDED, reason="runs 9.9.8")
    details = dataclasses.replace(details, info={"node": {"build": "b" * 40}})
    details = dataclasses.replace(details, view=dataclasses.replace(details.view, version="9.9.8"))
    envelope = node_readiness(
        details,
        key="k",
        worker_groups=[],
        app_version="9.9.9",
        app_build="a" * 40,
        file_checks=lambda url, key: pytest.fail("the Plex database must not be checked"),
    )
    row = _check(envelope, "node_version")
    assert (row["current"], row["recommended"]) == ("9.9.8", "9.9.9")


def test_a_worker_nodes_version_row_says_it_matches():
    row = _check(_envelope(_details()), "node_version")
    assert (row["ok"], row["label"]) == (True, "The node's version matches this app")


def test_a_failed_gpu_says_what_its_test_found_and_what_to_do():
    view_gpu = {**GPU, "status": "failed"}
    details = _details(gpus=(view_gpu,))
    details = reg.NodeDetails(**{**details.__dict__, "gpus": ({**view_gpu, "error": "CUDA acceleration test failed"},)})
    row = _check(_envelope(details), "node_gpu_cuda:0")
    assert row["reason"] == (
        "CUDA acceleration test failed. Check the GPU driver on the node, then Rescan GPUs on its Overview tab."
    )


def test_a_node_that_needs_an_update_links_the_steps():
    row = _check(_envelope(_details(reg.STATE_UPDATE_NEEDED)), "node_version")
    assert (row["docs_url"], row["help_label"]) == (UPDATE_DOCS_URL, "How to update a node")
    assert UPDATE_DOCS_URL.endswith("docs/guides.md#updating-a-node")
    assert "docs_url" not in _check(_envelope(_details()), "node_version")


def test_the_count_to_fix_matches_the_buckets_and_skips_the_plex_database_checks():
    failed = _details(roles=(ROLE_WORKER, ROLE_PLEX_DB), gpus=({**GPU, "status": "failed"},))
    envelope = node_readiness(failed, key="k", worker_groups=[], app_version="9.9.9", file_checks=None)
    assert "node_plex_db" not in [s["id"] for s in envelope["sections"]]
    # The failed GPU is a must fix; untested access and no worker group are recommended.
    assert to_fix(envelope) == {"must_fix": 1, "recommended": 2}
    assert to_fix(_envelope(_details(reg.STATE_DISABLED))) == {"must_fix": 0, "recommended": 0}
