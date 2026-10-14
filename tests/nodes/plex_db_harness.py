"""The Plex database role on a bare node app, and a ``requests``-shaped session that calls it in-process."""

from __future__ import annotations

from urllib.parse import urlsplit

import requests
from flask import Flask, jsonify

from media_preview_generator.nodes.server import plex_db as plex_db_role

TOKEN = "lab-shared-key-0123456789"
NODE_URL = "http://plex-host.lan:9494"


def node_app(config_dir: str, *, key: str = TOKEN, mountinfo_path: str = "/proc/self/mountinfo") -> Flask:
    """A bare node app with the Plex database role on, and a stand-in for the node server's own ``/v1/health``."""
    app = Flask(__name__)

    @app.get("/v1/health")
    def health():
        return jsonify({"ok": True, "agent": plex_db_role.agent_block()})

    plex_db_role.register_plex_db(app, plex_config_dir=config_dir, key=key, mountinfo_path=mountinfo_path)
    app.config["TESTING"] = True
    return app


class _FlaskResponse:
    """A ``requests``-shaped view of a Flask test response."""

    def __init__(self, response) -> None:
        self.status_code = response.status_code
        self._response = response

    def json(self):
        return self._response.get_json()


class FlaskSession:
    """A ``requests.Session`` that calls the node in-process, as the app's HTTP client would over the LAN.

    ``paths`` records every request, so a test can prove what was (or wasn't) sent.
    """

    def __init__(self, client) -> None:
        self._client = client
        self.stopped = False
        self.paths: list[str] = []

    def _check(self, url: str) -> str:
        if self.stopped:
            raise requests.ConnectionError("connection refused")
        path = urlsplit(url).path
        self.paths.append(path)
        return path

    def post(self, url, json=None, headers=None, timeout=None):
        return _FlaskResponse(self._client.post(self._check(url), json=json, headers=headers))

    def get(self, url, headers=None, timeout=None):
        return _FlaskResponse(self._client.get(self._check(url), headers=headers))
