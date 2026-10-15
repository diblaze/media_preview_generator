"""Worker groups with node devices: the grouped device picker, node chips and zero-capacity reasons."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ._mocks import mock_dashboard_defaults
from .test_worker_groups import _complete_setup, group_api, settings_page  # noqa: F401

pytestmark = pytest.mark.e2e

NODES = [
    {
        "id": "storage",
        "name": "storage",
        "state": "ready",
        "reason": "",
        "devices": [{"device": "cuda:0", "name": "Quadro P5000", "type": "nvidia", "status": "ok"}],
        "cpu_cores": 24,
    },
    {
        "id": "nuc",
        "name": "nuc",
        "state": "access_problem",
        "reason": "fix access first",
        "devices": [],
        "cpu_cores": 8,
    },
]


def offline_node_group(api: dict) -> None:
    state = api["state"]
    state["nodes"] = NODES + [
        {
            "id": "laptop",
            "name": "old-laptop",
            "state": "offline",
            "reason": "node offline",
            "devices": [{"device": "cuda:0", "name": "GTX 1650", "type": "nvidia", "status": "ok"}],
            "cpu_cores": 4,
        }
    ]
    state["groups"][1]["members"].append(
        {
            "id": "m-laptop",
            "resource": "gpu",
            "device": "cuda:0",
            "count": 1,
            "job_types": ["previews"],
            "node": "laptop",
        }
    )
    state["capacity"]["groups"][1]["members"].append(
        {
            "id": "m-laptop",
            "resource": "gpu",
            "device": "cuda:0",
            "node": "laptop",
            "node_name": "old-laptop",
            "reason": "node offline",
            "desired": 1,
            "target": 0,
            "available": 0,
            "busy": 0,
            "finishing": 0,
            "state": "node_unavailable",
        }
    )


def test_picker_lists_this_app_first_then_each_node_with_its_state(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    group_api["state"]["nodes"] = NODES
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    picker = page.locator('[data-member="m-cpu"] [data-pick]')
    labels = picker.locator("optgroup").evaluate_all("groups => groups.map(g => g.label)")
    assert labels == ["This app", "storage · Ready", "nuc · Access problem"]
    storage = picker.locator('optgroup[label="storage · Ready"] option')
    expect(storage).to_have_text(["storage · Quadro P5000", "storage · CPU (24 cores)"])
    picker.select_option("node:storage:gpu:cuda:0")
    row = page.locator('[data-member="m-cpu"]')
    expect(row.locator(".node-chip")).to_have_text("storage")
    expect(row.locator("[data-node-zero]")).to_have_count(0)
    expect(row).to_contain_text("Loudness removed — it runs on CPU only.")
    page.locator('[data-member="m-cpu"] [data-kind="previews"]').click()
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    member = group_api["writes"][-1][2]["groups"][0]["members"][0]
    assert member == {
        "id": "m-cpu",
        "resource": "gpu",
        "device": "cuda:0",
        "count": 2,
        "job_types": ["previews"],
        "node": "storage",
    }


def test_member_on_a_node_that_cannot_work_shows_why_it_has_zero(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    group_api["state"]["nodes"] = NODES
    settings_page(authed_page, app_url)
    page = authed_page
    page.locator('[data-edit="cpu-night"]').click()
    page.locator('[data-member="m-cpu"] [data-pick]').select_option("node:nuc:cpu")
    row = page.locator('[data-member="m-cpu"]')
    expect(row.locator(".node-chip")).to_have_text("nuc")
    expect(row.locator("[data-node-zero]")).to_have_text("fix access first · 0 now")
    # Red, as on the Nodes page: a node with an access problem gets no work at all.
    expect(row.locator("[data-node-zero]")).to_have_class("node-zero bad")
    expect(row).to_contain_text("nuc: fix access first · 0 workers now.")
    page.locator('[data-member="m-cpu"] [data-pick]').select_option("cpu")
    expect(row.locator(".node-chip")).to_have_count(0)
    page.locator("#workerGroupEditorApply").click()
    expect(page.locator("#workerGroupMessage")).to_contain_text("saved")
    assert "node" not in group_api["writes"][-1][2]["groups"][0]["members"][0]


def test_picker_without_nodes_stays_a_flat_list(authed_page: Page, app_url: str, group_api: dict) -> None:  # noqa: F811
    settings_page(authed_page, app_url)
    authed_page.locator('[data-edit="cpu-night"]').click()
    picker = authed_page.locator('[data-member="m-cpu"] [data-pick]')
    expect(picker.locator("optgroup")).to_have_count(0)
    expect(picker.locator("option")).to_have_text(["CPU", "NVIDIA card"])


def test_settings_row_and_editor_flag_an_offline_node_member(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    settings_page(authed_page, app_url)
    page = authed_page
    chip = page.locator('#workerGroupRows [data-group-id="gpu-video"] .mchip').nth(1)
    expect(chip).to_contain_text("old-laptop · GTX 1650")
    expect(chip).to_have_class("mchip is-missing")
    page.locator('[data-edit="gpu-video"]').click()
    zero = page.locator('[data-member="m-laptop"] [data-node-zero]')
    expect(zero).to_have_text("node offline · 0 now")
    expect(zero).to_have_class("node-zero bad")
    expect(page.locator('[data-member="m-laptop"] [data-pick]')).to_have_value("node:laptop:gpu:cuda:0")


def test_dashboard_shows_node_chip_zero_reason_and_offline_worker_card(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": 4,
        "worker_type": "GPU",
        "worker_name": "old-laptop · GPU Worker 1 (GTX 1650)",
        "group_name": "Video",
        "group_id": "gpu-video",
        "member_id": "m-laptop",
        "retiring": True,
        "status": "processing",
        "current_title": "The Bear · S02E06",
        "ffmpeg_started": True,
        "progress_percent": 40,
        "node": {"id": "laptop", "name": "old-laptop"},
        "node_state": "offline",
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    block = authed_page.locator('[data-member-block="gpu-video:m-laptop"]')
    expect(block.locator(".mem-h .node-chip")).to_have_text("old-laptop")
    expect(block.locator("[data-node-zero]")).to_have_text("node offline · 0 now")
    expect(block).to_contain_text("old-laptop · GTX 1650: node offline. Its jobs wait; other devices keep working.")
    card = authed_page.locator('[data-worker-key="GPU_4"]')
    expect(card.locator("[data-node-chip]")).to_have_text("old-laptop")
    expect(card.locator("[data-status-badge]")).to_have_text("Node offline")
    expect(card.locator("[data-status-badge]")).to_have_class("pill bad")
    local = authed_page.locator('[data-member-block="gpu-video:m-gpu"] .mem-h')
    expect(local.locator(".node-chip")).to_have_count(0)


@pytest.mark.parametrize(
    ("node_state", "text", "tone"),
    [
        ("update_needed", "Node update needed", "bad"),
        ("no_ffmpeg", "Node FFmpeg missing", "bad"),
        ("temp_problem", "Node temp folder problem", "bad"),
        ("disabled", "Node turned off", "warn"),
    ],
)
def test_a_worker_cards_node_state_has_the_nodes_pages_name_and_colour(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
    node_state: str,
    text: str,
    tone: str,
) -> None:
    """The dashboard showed these amber (red on the Nodes page) and a turned-off node as "Node disabled"."""
    offline_node_group(group_api)
    mock_dashboard_defaults(authed_page)
    worker = {
        "worker_id": 4,
        "worker_type": "GPU",
        "worker_name": "old-laptop · GPU Worker 1 (GTX 1650)",
        "group_name": "Video",
        "group_id": "gpu-video",
        "member_id": "m-laptop",
        "status": "idle",
        "node": {"id": "laptop", "name": "old-laptop"},
        "node_state": node_state,
    }
    authed_page.route("**/api/jobs/workers", lambda route: route.fulfill(json={"workers": [worker]}))
    authed_page.goto(app_url + "/")
    badge = authed_page.locator('[data-worker-key="GPU_4"] [data-status-badge]')
    expect(badge).to_have_text(text)
    expect(badge).to_have_class(f"pill {tone}")


def test_week_legend_names_a_node_device_apart_from_this_apps(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    settings_page(authed_page, app_url)
    legend = authed_page.locator("#workerWeekLegend")
    expect(legend).to_contain_text("old-laptop · ")
    expect(legend.locator("span", has_text="old-laptop · ")).to_have_count(1)


def test_the_dashboard_says_a_node_problem_once_and_links_the_node(
    authed_page: Page,
    app_url: str,
    group_api: dict,  # noqa: F811
) -> None:
    offline_node_group(group_api)
    group_api["state"]["warnings"] = [
        {
            "code": "node_unavailable",
            "group_id": "gpu-video",
            "member_id": "m-laptop",
            "message": "Video: old-laptop · GTX 1650 has no workers now (node offline). Its jobs wait; other devices "
            "keep working.",
        },
        {"code": "loudness_without_cpu", "message": "Loudness needs a CPU group."},
    ]
    mock_dashboard_defaults(authed_page)
    authed_page.goto(app_url + "/")
    block = authed_page.locator('[data-member-block="gpu-video:m-laptop"]')
    expect(block).to_contain_text("old-laptop · GTX 1650: node offline.")
    expect(block.get_by_role("link", name="Open old-laptop")).to_have_attribute("href", "/nodes#node-laptop")
    expect(authed_page.locator("#workerGroupLiveWarnings")).to_have_text("Loudness needs a CPU group.")
    expect(authed_page.get_by_text("has no workers now")).to_have_count(0)
