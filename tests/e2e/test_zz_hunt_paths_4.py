"""Hunt (paths #4): "Use this" on a Test access suggestion edits the first row whose NODE path equals the suggestion's
``from``, not the row covering the library. Two rows sharing one node folder (the natural mistake: both libraries
pointed at the node's media root, which is exactly when a real node can list the folder and suggest a fix) -> the
Movies suggestion overwrites the TV row.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .test_nodes_page import _library, _node, _node_tab, _open_node, _open_nodes


@pytest.fixture(scope="session", autouse=True)
def _complete_setup(complete_setup) -> None:
    return complete_setup


@pytest.mark.e2e
def test_use_this_edits_the_row_of_the_library_it_was_suggested_for(authed_page: Page, app_url: str) -> None:
    page = authed_page
    node = _node(
        "n-storage",
        "storage",
        path_mappings=[
            {"app_prefix": "/data/TV", "node_prefix": "/mnt/media"},
            {"app_prefix": "/data/Movies", "node_prefix": "/mnt/media"},
        ],
    )
    api = _open_nodes(page, app_url, [node])
    _open_node(page, "n-storage")
    pane = _node_tab(page, "media")
    api.test_answers = [
        {
            "libraries": [
                _library(
                    "TV",
                    "/data/TV",
                    False,
                    "/mnt/media",
                    nearby={"/mnt/media": ["Movies", "TV"]},
                    suggestion={"from": "/mnt/media", "to": "/mnt/media/TV"},
                ),
                _library(
                    "Movies",
                    "/data/Movies",
                    False,
                    "/mnt/media",
                    nearby={"/mnt/media": ["Movies", "TV"]},
                    suggestion={"from": "/mnt/media", "to": "/mnt/media/Movies"},
                ),
            ]
        },
        {"libraries": []},
    ]
    pane.locator(".nd-test").click()
    card = pane.locator('.nd-fail[data-library="Movies"]')
    expect(card).to_be_visible()

    with page.expect_response(lambda r: r.url.endswith("/test-access")):
        card.locator(".nd-use").click()

    nodes = pane.locator(".nd-node")
    expect(nodes.nth(1)).to_have_value("/mnt/media/Movies")  # the Movies row
    expect(nodes.nth(0)).to_have_value("/mnt/media")  # the TV row is left alone
