"""E2E against the real backend: the Nodes page talking to a real app process and a real node server.

The app runs as a subprocess (like ``backend_real_app``) with a Plex server whose two libraries are tmp folders of
tiny video files. The node is ``create_node_app`` served in-process by werkzeug on 127.0.0.1, with GPU detection
patched to report one NVIDIA GPU (the only fake: no machine running the suite is assumed to have a GPU). The node
sees the same files through "NAS" folders of hard links, so size and mtime match exactly as on a real share.

The Plex server's database selector is fed by a real Plex database node (``create_node_app(roles=["plex_db"])`` over a
tmp Plex folder holding an empty database file) and by an older Plex helper: a tiny in-process Flask app that answers
the helper's ``/v1/ping`` and nothing else, which the app probes, stores and lists exactly as it would a real one.

Screenshots go to ``MPG_UX_EVIDENCE_DIR`` when it is set (else the test's tmp dir).
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from flask import Flask, jsonify, request
from playwright.sync_api import BrowserContext, Locator, Page, expect
from werkzeug.serving import make_server

from .conftest import (
    _build_fake_ffmpeg_path,
    _capture_session_cookie,
    _seed_settings_complete,
    _start_app,
    accept_app_confirm,
    expect_modal_shown,
    get_free_port,
    watch_modal_shown,
)
from .test_intro_credits_server_tab import _switch_tab

NODE_KEY = "e2e-node-key-0123456789"
HELPER_KEY = "e2e-helper-key-0123456789"
NODE_NAME = "e2e-node"
PLEX_DB_NODE_NAME = "plex-box"
FAKE_GPU = ("NVIDIA", "cuda", {"name": "NVIDIA RTX 4000 (fake)"})


def _capture(locator: Locator, filename: str, tmp_path: Path) -> None:
    evidence = Path(os.environ.get("MPG_UX_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    locator.screenshot(path=str(evidence / filename), animations="disabled")


@contextmanager
def _serve(app: Flask) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, name="e2e-node-server", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _video(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x1a\x45\xdf\xa3" + b"\0" * size)


def _share(app_file: Path, app_media: Path, nas: Path) -> None:
    """The same file as the node sees it: a hard link, so size and mtime_ns are the app's own."""
    target = nas / app_file.relative_to(app_media)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(app_file, target)


@pytest.fixture
def media(tmp_path: Path) -> dict[str, Path]:
    """Two libraries as the app sees them, and the node's share holding the same files one level up."""
    app_media = tmp_path / "app" / "media"
    nas = tmp_path / "nas" / "share"
    files = [
        app_media / "Movies" / "Alpha (2020).mkv",
        app_media / "Movies" / "Beta (2021).mkv",
        app_media / "TV Shows" / "Show" / "Season 01" / "Show - S01E01.mkv",
    ]
    for i, path in enumerate(files):
        _video(path, 100 + i)
        _share(path, app_media, nas)
    return {
        "movies": app_media / "Movies",
        "tv": app_media / "TV Shows",
        "nas": nas,
    }


def _plex_server(media: dict[str, Path]) -> dict:
    return {
        "id": "plex-home",
        "type": "plex",
        "name": "Home",
        "enabled": True,
        # Port 9 refuses at once: nothing in these tests needs Plex itself.
        "url": "http://127.0.0.1:9",
        "auth": {"method": "token", "token": "x" * 20},
        "verify_ssl": True,
        "timeout": 5,
        "libraries": [
            {"id": "1", "name": "Movies", "remote_paths": [str(media["movies"])], "enabled": True},
            {"id": "2", "name": "TV Shows", "remote_paths": [str(media["tv"])], "enabled": True},
        ],
        "path_mappings": [],
        "exclude_paths": [],
        "output": {"adapter": "plex_bundle", "plex_config_folder": "/tmp", "frame_interval": 10},
    }


@pytest.fixture
def real_app(tmp_path_factory, media: dict[str, Path]) -> Generator[str, None, None]:
    """The app as a subprocess, set up, with the Plex server above."""
    config_dir = tmp_path_factory.mktemp("nodes_real_config")
    _seed_settings_complete(str(config_dir), {"media_servers": [_plex_server(media)]})
    fake_bin = _build_fake_ffmpeg_path(str(config_dir))
    env = {"PATH": fake_bin + os.pathsep + os.environ.get("PATH", ""), "CORS_ORIGINS": "*"}
    port = get_free_port()
    proc = _start_app(str(config_dir), port, extra_env=env)
    try:
        yield f"http://localhost:{port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def real_page(page: Page, context: BrowserContext, real_app: str) -> Page:
    context.add_cookies([_capture_session_cookie(real_app)])
    page.set_viewport_size({"width": 1440, "height": 1000})
    return page


@pytest.fixture
def node_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A real worker node on 127.0.0.1, reporting one (fake) NVIDIA GPU."""
    from media_preview_generator.gpu import detect
    from media_preview_generator.nodes.server.app import EXTENSION, create_node_app

    monkeypatch.setattr(detect, "detect_all_gpus", lambda: [FAKE_GPU])
    monkeypatch.setenv("NODE_NAME", NODE_NAME)
    monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "node-temp"))

    def never_called(ctx, node_path, params):  # noqa: ANN001, ANN202 - the op table must not be empty
        raise AssertionError("no task is started in these tests")

    app = create_node_app(
        config_dir=str(tmp_path / "node-config"), roles=["worker"], ops={"preview.frames": never_called}, key=NODE_KEY
    )
    try:
        with _serve(app) as url:
            yield url
    finally:
        app.extensions[EXTENSION].close()


@pytest.fixture
def plex_db_node_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A real Plex database node on 127.0.0.1 over a tmp Plex folder whose database file exists."""
    from media_preview_generator.markers.publishers.plex_db import plex_db_path
    from media_preview_generator.nodes.server.app import EXTENSION, create_node_app

    plex_dir = tmp_path / "Plex Media Server"
    database = Path(plex_db_path(str(plex_dir)))
    database.parent.mkdir(parents=True)
    database.write_bytes(b"")
    monkeypatch.setenv("NODE_NAME", PLEX_DB_NODE_NAME)
    monkeypatch.setenv("PLEX_CONFIG_DIR", str(plex_dir))
    app = create_node_app(config_dir=str(tmp_path / "plex-node-config"), roles=["plex_db"], key=NODE_KEY)
    try:
        with _serve(app) as url:
            yield url
    finally:
        app.extensions[EXTENSION].close()


@pytest.fixture
def helper_url() -> Iterator[str]:
    """An older Plex helper: answers its own ``/v1/ping`` with the agent block and 404s every node route."""
    app = Flask("e2e-plex-helper")

    @app.get("/v1/ping")
    def ping():  # noqa: ANN202
        agent = {"version": "1.2.0", "protocols": [1], "capabilities": []}
        if request.headers.get("Authorization") != f"Bearer {HELPER_KEY}":
            return jsonify({"ok": False, "agent": agent}), 401
        return jsonify({"ok": True, "agent": agent, "result": {}})

    with _serve(app) as url:
        yield url


def _open_add(page: Page) -> Locator:
    watch_modal_shown(page, "addNodeModal")
    page.locator("#nodesAddBtn").click()
    expect_modal_shown(page, "addNodeModal")
    return page.locator("#addNodeModal")


def _connect(page: Page, url: str, key: str) -> Locator:
    page.locator("#addNodeUrl").fill(url)
    page.locator("#addNodeKey").fill(key)
    page.locator("#addNodeConnect").click()
    probe = page.locator("#addNodeProbe")
    expect(probe).to_contain_text("✓ Connected", timeout=30000)
    return probe


def _node_tab(page: Page, tab: str) -> Locator:
    page.locator(f'#nodeModal .sm-rail [data-tab="{tab}"]').click()
    pane = page.locator(f"#node-tab-{tab}")
    expect(pane).to_be_visible()
    return pane


@pytest.mark.e2e
def test_a_worker_node_end_to_end(
    real_page: Page, real_app: str, node_url: str, media: dict[str, Path], tmp_path: Path
) -> None:
    page = real_page
    page.goto(f"{real_app}/nodes")
    expect(page.locator("#nodeList")).to_contain_text("No nodes yet", timeout=10000)

    # Add node, step 1: the probe shows what the real node reports.
    modal = _open_add(page)
    probe = _connect(page, node_url, NODE_KEY)
    expect(probe).to_contain_text("same as this app")
    expect(probe).to_contain_text("Worker")
    expect(probe).to_contain_text("cuda:0 · NVIDIA RTX 4000 (fake) · test decode ✓")
    expect(probe).to_contain_text("CPU cores")
    expect(probe).to_contain_text("free temp")
    expect(page.locator("#addNodeName")).to_have_value(NODE_NAME)

    # Step 2: one pre-filled row per library folder, the same path on the node.
    page.locator("#addNodeNext").click()
    expect(page.locator("#addNodeStep2")).to_be_visible()
    editor = page.locator("#addNodeMappings")
    rows = editor.locator(".nd-map-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0).locator(".nd-app")).to_have_value(str(media["movies"]))
    expect(rows.nth(0).locator(".nd-node")).to_have_value(str(media["movies"]))
    expect(rows.nth(1).locator(".nd-app")).to_have_value(str(media["tv"]))

    # Movies points at the share itself (the folder is one level down); TV Shows is right.
    rows.nth(0).locator(".nd-node").fill(str(media["nas"]))
    rows.nth(1).locator(".nd-node").fill(str(media["nas"] / "TV Shows"))
    editor.locator(".nd-test").click()
    expect(rows.nth(0).locator(".nd-chip-slot")).to_have_text("✗ Movies 0/2", timeout=15000)
    expect(rows.nth(1).locator(".nd-chip-slot")).to_have_text("✓ TV Shows 1/1")
    card = editor.locator('.nd-fail[data-library="Movies"]')
    expect(card).to_contain_text("Movies: not found on the node.")
    expect(card).to_contain_text(f"Tried {media['nas'] / 'Alpha (2020).mkv'}")
    expect(card).to_contain_text(f"Folders the node does see under {media['nas']}:")
    expect(card.locator(".pill")).to_have_text(["Movies", "TV Shows"])
    expect(card.locator(".nd-diff")).to_have_text(f"{media['nas']} → {media['nas'] / 'Movies'}")
    _capture(modal.locator(".modal-content"), "real-add-step2-failed.png", tmp_path)

    with page.expect_response(lambda r: r.url.endswith("/test-access")):
        card.locator(".nd-use").click()
    expect(rows.nth(0).locator(".nd-node")).to_have_value(str(media["nas"] / "Movies"))
    expect(rows.nth(0).locator(".nd-chip-slot")).to_have_text("✓ Movies 2/2")
    expect(editor.locator(".nd-map-results")).to_contain_text("The node sees every library, same files.")
    _capture(modal.locator(".modal-content"), "real-add-step2-fixed.png", tmp_path)

    page.locator("#addNodeFinish").click()
    expect(modal).to_be_hidden(timeout=10000)

    # The list: the real registry's state after a check.
    row = page.locator("#nodeList .nd-row").first
    expect(row.locator(".nd-name")).to_have_text(NODE_NAME)
    page.locator("#nodesCheckNow").click()
    expect(row.locator(".nd-status .pill:not(.nd-to-fix)")).to_have_text("✓ Ready", timeout=20000)
    expect(row.locator(".nd-chips")).to_contain_text("NVIDIA RTX 4000 (fake)")
    expect(row.locator(".nd-chips")).to_contain_text("idle")
    expect(row.locator(".nd-status")).to_contain_text("seen")
    _capture(page.locator(".nodes-page"), "real-nodes-page.png", tmp_path)

    # The node dialog: Overview.
    watch_modal_shown(page, "nodeModal")
    row.locator(".nd-open").click()
    expect_modal_shown(page, "nodeModal")
    overview = page.locator("#nodeOverview")
    expect(overview).to_contain_text("✓ Ready")
    expect(overview).to_contain_text("same as this app")
    expect(overview).to_contain_text("cores")
    expect(page.locator("#nodeGpuList")).to_contain_text("NVIDIA RTX 4000 (fake)")
    expect(page.locator("#nodeGpuList")).to_contain_text("✓ Working")

    # Media: the saved rows pass, which Setup Health then remembers.
    media_tab = _node_tab(page, "media")
    expect(media_tab.locator(".nd-node").first).to_have_value(str(media["nas"] / "Movies"))
    with page.expect_response(lambda r: r.url.endswith("/test-access")):
        media_tab.locator(".nd-test").click()
    expect(media_tab.locator(".nd-map-results")).to_contain_text("The node sees every library, same files.")

    # Setup Health: nothing to fix; not in a worker group yet is the one recommendation, with its link.
    _node_tab(page, "health")
    page.locator("#nodeHealthRefresh").click()
    expect(page.locator("#nodeHealthSummary .pill")).to_have_text(["1 recommended"], timeout=15000)
    recommended = page.locator('.readiness-bucket[data-tier="recommended"]')
    expect(recommended).to_contain_text("Not used in any worker group yet")
    expect(recommended.locator("a", has_text="Open Workers")).to_have_attribute("href", "/settings#section-workers")
    page.locator('.readiness-bucket[data-tier="ok"] summary').click()
    good = page.locator('.readiness-bucket[data-tier="ok"]')
    expect(good).to_contain_text(f"{NODE_NAME} answers")
    expect(good).to_contain_text("Home · Movies: the node sees its files")
    expect(good).to_contain_text("NVIDIA RTX 4000 (fake) works")
    expect(page.locator('.readiness-bucket[data-tier="critical"]')).to_have_count(0)
    _capture(page.locator("#nodeModal .modal-content"), "real-node-health.png", tmp_path)

    # Log: the node's own lines, as time, level, message.
    _node_tab(page, "log")
    expect(page.locator("#nodeLog")).to_contain_text(f"INFO Node {NODE_NAME}", timeout=10000)
    expect(page.locator("#nodeLog")).not_to_contain_text("{'time'")
    assert NODE_KEY not in page.locator("#nodeLog").inner_text()

    # Remove.
    _node_tab(page, "settings")
    page.locator("#nodeRemove").click()
    accept_app_confirm(page)
    expect(page.locator("#nodeModal")).to_be_hidden(timeout=10000)
    expect(page.locator("#nodeList")).to_contain_text("No nodes yet")


@pytest.mark.e2e
def test_a_node_added_twice_is_named_at_connect(real_page: Page, real_app: str, node_url: str) -> None:
    page = real_page
    page.goto(f"{real_app}/nodes")
    expect(page.locator("#nodeList")).to_contain_text("No nodes yet", timeout=10000)
    _open_add(page)
    _connect(page, node_url, NODE_KEY)
    page.locator("#addNodeNext").click()
    expect(page.locator("#addNodeStep2")).to_be_visible()
    page.locator("#addNodeCancel").click()
    expect(page.locator("#nodeList .nd-row")).to_have_count(1)

    _open_add(page)
    page.locator("#addNodeUrl").fill(node_url)
    page.locator("#addNodeKey").fill("not-the-key")
    page.locator("#addNodeConnect").click()
    expect(page.locator("#addNodeError")).to_contain_text("Key refused", timeout=15000)

    page.locator("#addNodeKey").fill(NODE_KEY)
    page.locator("#addNodeConnect").click()
    expect(page.locator("#addNodeError")).to_contain_text(f'This node is already added as "{NODE_NAME}"', timeout=15000)
    expect(page.locator("#addNodeNext")).to_be_hidden()


def _open_node_settings(page: Page) -> None:
    watch_modal_shown(page, "nodeModal")
    page.locator("#nodeList .nd-row .nd-open").first.click()
    expect_modal_shown(page, "nodeModal")
    _node_tab(page, "settings")


@pytest.mark.e2e
def test_a_tab_opened_before_another_tabs_save_cant_undo_it(
    real_page: Page, context: BrowserContext, real_app: str, node_url: str
) -> None:
    tab_a = real_page
    tab_a.goto(f"{real_app}/nodes")
    expect(tab_a.locator("#nodeList")).to_contain_text("No nodes yet", timeout=10000)
    _open_add(tab_a)
    _connect(tab_a, node_url, NODE_KEY)
    tab_a.locator("#addNodeNext").click()
    expect(tab_a.locator("#addNodeStep2")).to_be_visible()
    # Finish later saves the rows step 2 shows: the add flow's own save goes through.
    with tab_a.expect_response(lambda r: r.request.method == "PUT" and "/api/nodes/" in r.url) as kept:
        tab_a.locator("#addNodeCancel").click()
    assert kept.value.status == 200

    tab_b = context.new_page()
    tab_b.set_viewport_size({"width": 1440, "height": 1000})
    tab_b.goto(f"{real_app}/nodes")
    expect(tab_b.locator("#nodeList .nd-row")).to_have_count(1, timeout=10000)
    _open_node_settings(tab_b)

    _open_node_settings(tab_a)
    tab_a.locator("#nodeSetName").fill("e2e-node-renamed")
    tab_a.locator("#nodeSettingsSave").click()
    expect(tab_a.locator("#nodeSettingsResult")).to_have_text("Saved", timeout=15000)

    # Tab B still shows the old name; switching the node off there would save that name too.
    expect(tab_b.locator("#nodeSetName")).to_have_value(NODE_NAME)
    tab_b.locator("label[for='nodeSetEnabled']").click()
    tab_b.locator("#nodeSettingsSave").click()

    expect(tab_b.locator("#nodeSettingsResult")).to_have_text(
        "This node was changed in another tab. Reload the page, then make your change again.", timeout=15000
    )
    (stored,) = tab_a.request.get(f"{real_app}/api/nodes").json()["nodes"]
    assert (stored["name"], stored["enabled"]) == ("e2e-node-renamed", True)

    # Reloaded, tab B saves its change on top of tab A's.
    tab_b.goto(f"{real_app}/nodes")
    expect(tab_b.locator("#nodeList .nd-row .nd-name")).to_have_text("e2e-node-renamed", timeout=10000)
    _open_node_settings(tab_b)
    tab_b.locator("label[for='nodeSetEnabled']").click()
    tab_b.locator("#nodeSettingsSave").click()
    expect(tab_b.locator("#nodeSettingsResult")).to_have_text("Saved", timeout=15000)
    (stored,) = tab_b.request.get(f"{real_app}/api/nodes").json()["nodes"]
    assert (stored["name"], stored["enabled"]) == ("e2e-node-renamed", False)


@pytest.mark.e2e
def test_plex_database_nodes_are_added_and_offered_to_the_plex_server(
    real_page: Page, real_app: str, plex_db_node_url: str, helper_url: str, tmp_path: Path
) -> None:
    page = real_page
    page.goto(f"{real_app}/nodes")
    expect(page.locator("#nodeList")).to_contain_text("No nodes yet", timeout=10000)

    # A Plex database node: no media step, its database facts in the probe.
    modal = _open_add(page)
    probe = _connect(page, plex_db_node_url, NODE_KEY)
    expect(probe).to_contain_text("Plex database")
    expect(probe).not_to_contain_text("Worker")
    expect(probe).not_to_contain_text("same as this app")
    expect(probe).to_contain_text("✓ Database found")
    expect(probe).to_contain_text(str(tmp_path / "Plex Media Server"))
    expect(page.locator("#addNodeName")).to_have_value(PLEX_DB_NODE_NAME)
    expect(page.locator("#addNodeNext")).to_have_text("Add node")
    _capture(modal.locator(".modal-content"), "real-add-plex-db-node.png", tmp_path)
    page.locator("#addNodeNext").click()
    expect(modal).to_be_hidden(timeout=10000)

    # An older Plex helper: it has no name of its own, so its host stands in.
    modal = _open_add(page)
    probe = _connect(page, helper_url, HELPER_KEY)
    expect(probe).to_contain_text("version 1.2.0")
    expect(probe).to_contain_text("Older Plex database node")
    page.locator("#addNodeNext").click()
    expect(modal).to_be_hidden(timeout=10000)

    page.locator("#nodesCheckNow").click()
    plex_box = page.locator("#nodeList .nd-row", has_text=PLEX_DB_NODE_NAME)
    helper = page.locator("#nodeList .nd-row", has_text="127.0.0.1").filter(has_not_text=PLEX_DB_NODE_NAME)
    expect(plex_box.locator(".nd-status .pill:not(.nd-to-fix)")).to_have_text("✓ Ready", timeout=20000)
    expect(plex_box.locator(".nd-ico i")).to_have_class("bi bi-database")
    expect(helper.locator(".nd-status .pill:not(.nd-to-fix)")).to_have_text("✓ Ready", timeout=20000)
    expect(helper.locator(".nd-chips")).to_contain_text("Older Plex database node")

    page.goto(f"{real_app}/servers")
    edit = page.locator(".edit-server-btn[data-id='plex-home']")
    edit.wait_for(state="visible", timeout=10000)
    watch_modal_shown(page, "editServerModal")
    edit.click()
    expect_modal_shown(page, "editServerModal")
    _switch_tab(page, "general")
    expect(page.locator("#markersPlexDatabase option")).to_have_text(
        [
            "Open it from this app",
            f"Through a Plex database node: {PLEX_DB_NODE_NAME}",
            "Through a Plex database node: 127.0.0.1",
        ],
        timeout=10000,
    )
