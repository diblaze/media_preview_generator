"""Browser-to-Flask loudness contract; only external media-server calls are mocked."""

# ruff: noqa: F811 - imported pytest fixtures are used as test parameters.

from __future__ import annotations

from threading import Thread
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect
from werkzeug.serving import make_server

from tests.test_inspector_loudness import (  # noqa: F401 - use the native XML fixture and real routes
    _reset_singletons,
    app,
    authed_client,
    client,
    film,
    media,
    native,
)


@pytest.mark.e2e
def test_native_xml_reaches_browser_through_real_inspector_route(page: Page, app, authed_client, film, native) -> None:
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    cookie = authed_client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    page.context.add_cookies([{"name": cookie.key, "value": cookie.value, "url": url}])
    try:
        page.goto(f"{url}/inspector?path={quote(film)}")
        card = page.locator("#inspLoudness [data-server-id='plex-loudness']")
        expect(card).to_contain_text("Available in Plex")
        expect(card).to_contain_text("Loudness analysis by this app is off")
        expect(card.locator("dd")).to_have_text(["-23.1 LUFS", "-2.5 dBTP", "8.2 LU", "-33.6 LUFS", "0.4 dB"])
        expect(card).to_contain_text("Normalization available")
        assert native.query.called
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
