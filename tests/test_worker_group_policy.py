"""Policy boundaries, including overnight time and overlapping capacity."""

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from media_preview_generator.worker_groups import (
    configured_group_totals,
    effective_worker_groups,
    group_is_available,
    next_group_opening,
    supports_job,
    validate_worker_groups,
)


def cpu_group(**changes):
    return {
        "id": "cpu-a",
        "name": "Loudness",
        "enabled": True,
        "resource": "cpu",
        "device": None,
        "count": 2,
        "job_types": ["loudness"],
        "availability": {"mode": "always", "windows": []},
        **changes,
    }


def window(days, start="23:00", end="07:00"):
    return {"mode": "scheduled", "windows": [{"days": days, "start": start, "end": end}]}


@pytest.mark.parametrize(
    ("timestamp", "eligible"),
    [
        ("2026-10-05T01:00:00", False),
        ("2026-10-05T22:59:59", False),
        ("2026-10-05T23:00:00", True),
        ("2026-10-06T06:59:59", True),
        ("2026-10-06T07:00:00", False),
    ],
)
def test_overnight_belongs_to_start_day(timestamp, eligible):
    group = cpu_group(availability=window([0]))
    assert group_is_available(group, datetime.fromisoformat(timestamp)) is eligible


def test_sunday_window_wraps_into_monday():
    group = cpu_group(availability=window([6]))
    assert group_is_available(group, datetime(2026, 10, 5, 1))
    assert not group_is_available(group, datetime(2026, 10, 6, 1))


def test_same_group_windows_union_but_different_groups_add():
    group = cpu_group(count=20, availability=window([0], "08:00", "12:00"))
    group["availability"]["windows"].append({"days": [0], "start": "10:00", "end": "14:00"})
    assert configured_group_totals(validate_worker_groups([group])) == (0, 20)
    other = {**group, "id": "cpu-b"}
    with pytest.raises(ValueError, match="Overlapping"):
        validate_worker_groups([group, other])


def test_nonoverlapping_groups_share_capacity_limit():
    groups = [
        cpu_group(count=32, availability=window([0], "08:00", "12:00")),
        cpu_group(id="cpu-b", count=32, availability=window([0], "12:00", "16:00")),
    ]
    assert configured_group_totals(validate_worker_groups(groups)) == (0, 32)


def test_gpu_family_limit_applies_across_devices():
    groups = [
        cpu_group(id=f"gpu-{n}", resource="gpu", device=f"gpu:{n}", count=20, job_types=["previews"]) for n in range(2)
    ]
    with pytest.raises(ValueError, match="GPU 40/32"):
        validate_worker_groups(groups)


def test_cpu_loudness_requirement_cannot_be_overridden_by_policy():
    group = cpu_group(resource="gpu", device="gpu:0")
    assert not supports_job(group, "loudness")
    with pytest.raises(ValueError, match="requires CPU"):
        validate_worker_groups([group])


def test_policy_allows_only_selected_job_kinds():
    group = cpu_group()
    assert supports_job(group, "loudness")
    assert not supports_job(group, "previews")
    assert not supports_job(group, "intro_credits")


@pytest.mark.parametrize(
    "change",
    [
        {"count": True},
        {"count": 0},
        {"count": 33},
        {"job_types": []},
        {"job_types": ["chapters"]},
        {"enabled": "false"},
        {"availability": window([], "01:00", "02:00")},
        {"availability": window([0], "01:00", "01:00")},
        {"availability": window([0], "25:00", "02:00")},
    ],
)
def test_invalid_policy_is_rejected(change):
    with pytest.raises(ValueError):
        validate_worker_groups([cpu_group(**change)])


def test_explicit_empty_policy_does_not_restore_legacy_counts():
    assert effective_worker_groups({"worker_groups": [], "cpu_threads": 8}) == []
    assert effective_worker_groups({"cpu_threads": 0, "gpu_config": []}) == []
    assert effective_worker_groups({"cpu_threads": 2})[0]["count"] == 2


def test_disabled_group_has_no_opening_and_does_not_use_budget():
    group = cpu_group(enabled=False, count=32)
    assert not group_is_available(group)
    assert next_group_opening(group) is None
    assert configured_group_totals([group]) == (0, 0)


def test_spring_gap_returns_first_real_open_minute():
    zone = ZoneInfo("Australia/Sydney")
    group = cpu_group(availability=window([6], "02:30", "04:00"))
    opening = next_group_opening(group, datetime(2026, 10, 4, 1, 59, tzinfo=zone))
    assert opening == datetime(2026, 10, 4, 3, 0, tzinfo=zone)


def test_repeated_hour_is_available_in_both_folds():
    zone = ZoneInfo("Australia/Sydney")
    group = cpu_group(availability=window([6], "02:00", "03:00"))
    assert group_is_available(group, datetime(2026, 4, 5, 2, 30, tzinfo=zone, fold=0))
    assert group_is_available(group, datetime(2026, 4, 5, 2, 30, tzinfo=zone, fold=1))


def test_validation_does_not_mutate_draft():
    group = cpu_group()
    original = copy.deepcopy(group)
    clean = validate_worker_groups([group])
    clean[0]["job_types"].append("previews")
    assert group == original


def test_group_revision_and_persistence_failure_preserve_saved_policy(tmp_path, monkeypatch):
    from media_preview_generator.web.settings_manager import SettingsManager, WorkerGroupsConflict

    settings = SettingsManager(str(tmp_path))
    assert settings.update_worker_groups([cpu_group()], expected_revision=0) == 1
    with pytest.raises(WorkerGroupsConflict):
        settings.update_worker_groups([], expected_revision=0)
    assert settings.worker_groups[0]["count"] == 2

    def fail_save():
        raise OSError("Disk full")

    monkeypatch.setattr(settings, "_save", fail_save)
    with pytest.raises(OSError, match="Disk full"):
        settings.update_worker_groups([], expected_revision=1)
    assert settings.worker_groups_revision == 1
    assert settings.worker_groups[0]["count"] == 2


def test_global_pause_owners_do_not_clear_each_other(tmp_path):
    from media_preview_generator.web.settings_manager import SettingsManager

    settings = SettingsManager(str(tmp_path))
    settings.processing_paused = True
    settings.set_processing_pause_reason("quiet_hours", True)
    settings.set_processing_pause_reason("quiet_hours", False)
    assert settings.processing_paused
    assert settings.processing_pause_reasons == ["manual"]
    settings.set_processing_pause_reason("quiet_hours", True)
    settings.processing_paused = False
    assert settings.processing_paused
    assert settings.processing_pause_reasons == ["quiet_hours"]


def test_fully_skipped_dst_window_opens_the_following_week():
    zone = ZoneInfo("Australia/Sydney")
    group = cpu_group(availability=window([6], "02:15", "02:30"))
    # September 27 has already closed, and October 4's entire window does not
    # exist when clocks advance. The next real window is two Sundays away.
    opening = next_group_opening(group, datetime(2026, 9, 27, 3, 0, tzinfo=zone))
    assert opening == datetime(2026, 10, 11, 2, 15, tzinfo=zone)
