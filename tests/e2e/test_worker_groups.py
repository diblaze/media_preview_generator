"""Worker group UI contracts through the real settings/dashboard templates."""

from __future__ import annotations

import copy

import pytest
from playwright.sync_api import Page, expect

from ._mocks import (
    capture_settings_save,
    mock_dashboard_defaults,
    mock_settings_backups,
    mock_settings_get,
    mock_setup_status,
    mock_system_status,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.fixture
def group_api(authed_page: Page) -> dict:
    state = {
        "groups": [
            {
                "id": "cpu-night",
                "name": "Overnight loudness",
                "enabled": True,
                "resource": "cpu",
                "device": None,
                "count": 2,
                "job_types": ["loudness"],
                "availability": {"mode": "scheduled", "windows": [{"days": [0], "start": "23:00", "end": "07:00"}]},
            },
            {
                "id": "gpu-video",
                "name": 'Video <img src=x onerror="window.injected=1">',
                "enabled": True,
                "resource": "gpu",
                "device": "nvidia0",
                "count": 2,
                "job_types": ["previews", "intro_credits"],
                "availability": {"mode": "always", "windows": []},
            },
        ],
        "revision": 7,
        "timezone": "Australia/Sydney",
        "limits": {"cpu": 32, "gpu": 32},
        "hardware": [{"device": "nvidia0", "name": "NVIDIA card", "type": "nvidia", "status": "ok"}],
        "capacity": {
            "groups": [
                {
                    "id": "cpu-night",
                    "desired": 2,
                    "available": 0,
                    "busy": 2,
                    "finishing": 0,
                    "state": "busy",
                    "next_available_at": None,
                },
                {
                    "id": "gpu-video",
                    "desired": 2,
                    "available": 1,
                    "busy": 1,
                    "finishing": 0,
                    "state": "available",
                    "next_available_at": None,
                },
            ]
        },
        "warnings": [],
    }
    control = {"state": state, "writes": [], "conflict": False}

    def route(request):
        method = request.request.method
        if method == "GET":
            request.fulfill(json=control["state"])
            return
        body = request.request.post_data_json
        control["writes"].append((method, request.request.url, body))
        if control["conflict"] or method == "PUT" and body["revision"] != state["revision"]:
            request.fulfill(status=409, json={"error": "Groups changed elsewhere"})
            return
        if method == "PUT":
            state["groups"] = copy.deepcopy(body["groups"])
        else:
            group_id = request.request.url.split("/")[-2]
            group = next(g for g in state["groups"] if g["id"] == group_id)
            runtime = next(g for g in state["capacity"]["groups"] if g["id"] == group_id)
            if "enabled" in body:
                group["enabled"] = body["enabled"]
            else:
                desired = (group["count"] if group["enabled"] else 0) + body["delta"]
                if desired <= 0:
                    group["enabled"] = False
                else:
                    group["count"] = desired
                    group["enabled"] = True
            runtime.update(
                desired=group["count"] if group["enabled"] else 0,
                busy=min(2, group["count"]) if group["enabled"] else 0,
                finishing=max(0, 2 - group["count"]) if group["enabled"] else 2,
                state="busy" if group["enabled"] else "disabled",
            )
        state["revision"] += 1
        request.fulfill(json=state)

    authed_page.route("**/api/worker-groups**", route)
    return control


def settings_page(page: Page, url: str) -> list:
    mock_settings_get(page)
    mock_setup_status(page, complete=True)
    mock_system_status(page)
    mock_settings_backups(page)
    captured = capture_settings_save(page)
    page.goto(url + "/settings")
    expect(page.locator('#workerGroupRows [data-group-id="cpu-night"]')).to_be_visible()
    return captured


def test_settings_draft_apply_payload_and_no_legacy_counts(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    expect(page.locator("#workerGroupEditor")).to_be_hidden()
    expect(page.locator("#cpuThreads, .gpu-workers, .gpu-enable-toggle")).to_have_count(0)
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupName").fill("CPU audio")
    page.locator("#workerGroupCount").fill("3")
    page.locator('[data-day="1"]').check()
    assert group_api["writes"] == []
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    method, _, payload = group_api["writes"][0]
    assert method == "PUT"
    assert payload["revision"] == 7
    assert payload["groups"][0]["count"] == 3
    assert payload["groups"][0]["name"] == "CPU audio"
    assert payload["groups"][0]["job_types"] == ["loudness"]
    assert payload["groups"][0]["availability"]["windows"][0] == {"days": [0, 1], "start": "23:00", "end": "07:00"}
    expect(page.locator("#workerGroupEditor")).to_be_hidden()
    page.evaluate("saveAllSettings()")
    # Hardware tuning remains separate from the authoritative group counts.
    assert page.evaluate("collectGpuConfig().every(g => !('workers' in g) && !('enabled' in g))")
    assert page.evaluate("window.injected") is None


def test_invalid_inputs_and_gpu_capability_never_write(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupCount").fill("0")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("Disable a group")
    page.locator("#workerGroupCount").fill("2")
    page.locator("#wgEnd0").fill("23:00")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("different valid")
    page.locator("#workerGroupResource").select_option("nvidia0")
    expect(page.locator('[data-kind="loudness"]')).to_be_disabled()
    expect(page.locator('[data-kind="loudness"]')).not_to_be_checked()
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("at least one job")
    assert not group_api["writes"]


def test_revision_conflict_preserves_draft_and_discard_reloads(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupName").fill("My draft")
    group_api["state"]["revision"] = 8
    group_api["state"]["groups"][0]["count"] = 5
    page.evaluate("WorkerGroups.load()")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("draft is preserved")
    expect(page.locator("#workerGroupName")).to_have_value("My draft")
    assert group_api["writes"][0][2]["revision"] == 7
    page.locator("#workerGroupCancel").click()
    expect(page.locator('#workerGroupRows [data-group-id="cpu-night"]')).to_contain_text("5 desired")
    page.locator('[data-edit="cpu-night"]').click()
    expect(page.locator("#workerGroupName")).to_have_value("Overnight loudness")


def test_dashboard_scale_is_group_specific_and_finishing_is_separate(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.goto(app_url + "/")
    row = page.locator('#workerGroupDashboard [data-group-id="cpu-night"]')
    expect(row).to_contain_text("2 running")
    row.locator('[data-scale="-1"]').click()
    expect(row).to_contain_text("1 finishing")
    expect(row.locator("output")).to_have_text("1")
    assert group_api["writes"][0][1].endswith("/cpu-night/scale")
    assert group_api["writes"][0][2] == {"delta": -1}
    row.locator('[data-scale="-1"]').click()
    expect(row).to_contain_text("Disabled")
    expect(row).to_contain_text("2 finishing")
    expect(row.locator("output")).to_have_text("0")
    assert group_api["state"]["groups"][0]["count"] == 1
    assert group_api["state"]["groups"][1]["count"] == 2
    row.locator("[data-enable]").check()
    expect(row.locator("output")).to_have_text("1")
    assert group_api["writes"][-1][2] == {"enabled": True}
    assert not any("/api/settings" in path for _, path, _ in group_api["writes"])


def test_quiet_hours_has_one_editor_and_schedules_links_to_it(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    expect(page.locator("#quietHoursSaveBtn")).to_have_count(1)
    expect(page.locator("#section-worker-quiet-hours")).to_contain_text("scheduled runs are not caught up")
    page.goto(app_url + "/automation?tab=schedules")
    expect(page.locator("#quietHoursSaveBtn")).to_have_count(0)
    expect(page.locator('#section-schedules-quiet-hours a[href="/settings#section-worker-quiet-hours"]')).to_have_count(
        1
    )


@pytest.mark.parametrize(
    ("state", "enabled", "expected"),
    [
        ("active", True, "Within group hours"),
        ("off_hours", True, "Outside hours"),
        ("draining", True, "Outside hours"),
        ("hardware_unavailable", True, "Hardware unavailable"),
        ("disabled", False, "Disabled"),
    ],
)
def test_worker_wait_states_remain_distinct(
    authed_page: Page, app_url: str, group_api: dict, state: str, enabled: bool, expected: str
) -> None:
    mock_dashboard_defaults(authed_page)
    group_api["state"]["groups"][0]["enabled"] = enabled
    row_data = group_api["state"]["capacity"]["groups"][0]
    row_data.update(state=state, busy=0, finishing=0)
    # An open group may report its current opening; that is not a future appointment.
    row_data["next_available_at"] = "2099-10-05T23:00:00+11:00"
    authed_page.goto(app_url + "/")
    row = authed_page.locator('[data-group-id="cpu-night"]')
    expect(row).to_contain_text(expected)
    if state in ("off_hours", "draining"):
        expect(row).to_contain_text("Next")
        expect(row).to_contain_text("Australia/Sydney")
        expect(row.locator("output")).to_have_text("2")
    else:
        expect(row.locator(".worker-group-state")).not_to_contain_text("Next")


def test_global_pause_and_removed_draining_group_remain_visible(
    authed_page: Page, app_url: str, group_api: dict
) -> None:
    mock_dashboard_defaults(authed_page)
    group_api["state"].update(processing_paused=True, pause_reasons=["manual", "quiet_hours"])
    group_api["state"]["capacity"]["groups"].append(
        {"id": "removed", "name": "Old audio", "resource": "cpu", "busy": 0, "finishing": 1}
    )
    authed_page.goto(app_url + "/")
    expect(authed_page.locator("#workerGroupHold")).to_contain_text("manual pause and global pause schedule")
    expect(authed_page.locator('[data-group-id="cpu-night"]')).to_contain_text("2 paused")
    expect(authed_page.locator('[data-retired-group="removed"]')).to_contain_text("1 finishing (paused)")
    expect(authed_page.locator('[data-retired-group="removed"] button')).to_have_count(0)


def test_saving_window_union_and_add_duplicate_remove(authed_page: Page, app_url: str, group_api: dict) -> None:
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator("#workerGroupAddWindow").click()
    page.locator("#wgStart1").fill("22:00")
    page.locator("#workerGroupDuplicate").click()
    page.locator("#workerGroupName").fill("Other hours")
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    payload = group_api["writes"][-1][2]
    assert len(payload["groups"]) == 3
    assert len(payload["groups"][0]["availability"]["windows"]) == 2
    duplicate = payload["groups"][-1]
    assert duplicate["id"] != "cpu-night"
    assert duplicate["name"] == "Other hours"
    page.locator('[data-edit="' + duplicate["id"] + '"]').click()
    page.locator("#workerGroupRemove").click()
    page.locator("#workerGroupApply").click()
    expect(page.locator("#workerGroupRows .worker-group-row")).to_have_count(2)
    assert len(group_api["writes"][-1][2]["groups"]) == 2


def test_api_failure_keeps_configuration_uneditable(authed_page: Page, app_url: str) -> None:
    authed_page.route("**/api/worker-groups", lambda route: route.fulfill(status=503, json={"error": "Unavailable"}))
    mock_settings_get(authed_page)
    mock_system_status(authed_page)
    authed_page.goto(app_url + "/settings")
    expect(authed_page.locator("#workerGroupSettings")).to_contain_text("Could not load")
    expect(authed_page.locator("#workerGroupAdd")).to_have_count(0)


def test_worker_card_names_group_and_retiring_phase(authed_page: Page, app_url: str, group_api: dict) -> None:
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": "CPU-1",
        "worker_type": "CPU",
        "worker_name": "CPU Worker 1",
        "group_name": "Night audio",
        "retiring": True,
        "status": "processing",
        "current_title": "Movie",
        "current_phase": "Loudness 1/1",
        "ffmpeg_started": False,
        "progress_percent": 0,
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    expect(authed_page.locator("[data-worker-group]")).to_have_text("Night audio")
    expect(authed_page.locator("[data-status-badge]")).to_have_text("Finishing current file")
    expect(authed_page.locator("[data-title]")).to_contain_text("Movie")


def test_resume_processing_keeps_server_reported_quiet_hold(authed_page: Page, app_url: str, group_api: dict) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    page.route(
        "**/api/processing/state",
        lambda route: route.fulfill(json={"paused": True, "reasons": ["quiet_hours", "manual"]}),
    )
    page.route(
        "**/api/processing/resume", lambda route: route.fulfill(json={"paused": True, "reasons": ["quiet_hours"]})
    )
    group_api["state"].update(processing_paused=True, pause_reasons=["quiet_hours"])
    page.goto(app_url + "/")
    page.get_by_role("button", name="Resume all processing", exact=True).click()
    expect(page.locator("#toastBody")).to_contain_text("global pause schedule is still active")
    expect(page.get_by_role("button", name="Resume all processing", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Pause all processing, including current files", exact=True)).to_have_count(
        0
    )


@pytest.mark.parametrize("status", ["pending", "running"])
def test_preview_job_pause_is_independent_and_schedule_resume_remains_held(
    authed_page: Page, app_url: str, group_api: dict, status: str
) -> None:
    page = authed_page
    mock_dashboard_defaults(page)
    job = {
        "id": "preview-pause",
        "kind": "previews",
        "status": status,
        "library_name": "Movies",
        "config": {},
        "paused": False,
        "priority": 2,
        "created_at": "2026-10-05T01:00:00Z",
        "progress": {"percent": 0, "total_items": 1, "processed_items": 0, "current_item": "Waiting"},
    }
    calls = []
    page.route("**/api/jobs?**", lambda route: route.fulfill(json={"jobs": [job], "total": 1, "page": 1}))

    def change(route):
        calls.append(route.request.url)
        job["paused"] = True
        job["config"] = {"pause_reasons": ["schedule"]}
        route.fulfill(json={**job, "processing_paused": False})

    page.route("**/api/jobs/preview-pause/pause", change)
    page.route("**/api/jobs/preview-pause/resume", change)
    page.route("**/api/processing/pause", lambda route: (calls.append("GLOBAL"), route.fulfill(json={"paused": True})))
    page.goto(app_url + "/")
    row = page.locator("#job-row-preview-pause")
    row.get_by_role("button", name="Pause job", exact=True).click()
    expect(row.get_by_role("button", name="Resume job", exact=True)).to_be_visible()
    page.locator("#toastNotification .btn-close").click()
    expect(page.locator("#toastNotification")).to_be_hidden()
    row.get_by_role("button", name="Resume job", exact=True).click()
    expect(page.locator("#toastBody")).to_contain_text("still paused by its schedule")
    assert calls == [app_url + "/api/jobs/preview-pause/pause", app_url + "/api/jobs/preview-pause/resume"]


@pytest.mark.parametrize(("width", "theme"), [(1440, "dark"), (390, "light")])
def test_worker_group_editor_and_dashboard_fit_supported_sizes(
    authed_page: Page, app_url: str, group_api: dict, width: int, theme: str
) -> None:
    import os
    from pathlib import Path

    page = authed_page
    page.set_viewport_size({"width": width, "height": 1000})
    group_api["state"]["groups"][1]["name"] = "NVIDIA previews"
    screenshots = os.environ.get("WORKER_GROUP_SCREENSHOTS")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    settings_page(page, app_url)
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    page.evaluate("document.fonts.ready")
    expect(page.locator("#workerGroupEditor")).to_be_hidden()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshots:
        Path(screenshots).mkdir(parents=True, exist_ok=True)
        page.locator("#section-workers").screenshot(path=str(Path(screenshots) / f"workers-{width}-{theme}.png"))
    page.locator('[data-edit="cpu-night"]').click()
    expect(page.locator("#workerGroupApplyRow")).to_be_hidden()
    expect(page.locator("#workerGroupName")).to_be_focused()
    expect(page.locator("#workerGroupWindows")).to_contain_text("ends Tuesday")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshots:
        page.locator("#section-workers").screenshot(path=str(Path(screenshots) / f"editor-{width}-{theme}.png"))
    page.locator("#workerGroupClose").click()
    mock_dashboard_defaults(page)
    page.goto(app_url + "/")
    page.evaluate("theme => document.documentElement.dataset.bsTheme = theme", theme)
    expect(page.locator("#workerGroupDashboard")).to_contain_text("Overnight loudness")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if screenshots:
        page.locator(".dashboard-system-card").screenshot(
            path=str(Path(screenshots) / f"dashboard-{width}-{theme}.png")
        )
    assert errors == []
