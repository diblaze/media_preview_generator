"""What the images do at run time, read from their Dockerfiles: the HEALTHCHECK's port, the Plex database image's
role guard."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "Dockerfile"
PLEX_DB = ROOT / "nodes" / "plex-db" / "Dockerfile"
# Runs the HEALTHCHECK's Python with urlopen printing the URL it was given instead of calling it.
_RECORD_URL = "import sys, urllib.request; urllib.request.urlopen = lambda url, **_: print(url); exec(sys.argv[1])"


def _instructions(dockerfile: Path) -> list[str]:
    joined = dockerfile.read_text().replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _healthcheck_code(dockerfile: Path) -> str:
    [line] = [line for line in _instructions(dockerfile) if line.startswith("HEALTHCHECK ")]
    command = shlex.split(line.split(" CMD ", 1)[1])
    assert command[:3] == ["exec", "python3", "-c"], command
    return command[3]


def _url(dockerfile: Path, **env: str) -> str:
    clean = {k: v for k, v in os.environ.items() if k not in ("MODE", "NODE_PORT", "WEB_PORT", "AGENT_PORT")}
    done = subprocess.run(
        [sys.executable, "-c", _RECORD_URL, _healthcheck_code(dockerfile)],
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return done.stdout.strip()


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({"MODE": "node"}, "http://localhost:9494/v1/health"),
        # wrapper.sh reads an empty NODE_PORT as the default (${NODE_PORT:-9494}); the check must look there too.
        ({"MODE": "node", "NODE_PORT": ""}, "http://localhost:9494/v1/health"),
        ({"MODE": "node", "NODE_PORT": "9500"}, "http://localhost:9500/v1/health"),
        # The node strips its port: an env file saved with CRLF, or spaces, still serve there (nodes/server/wsgi.py).
        ({"MODE": "node", "NODE_PORT": "9500\r"}, "http://localhost:9500/v1/health"),
        ({"MODE": "node", "NODE_PORT": " 9500 "}, "http://localhost:9500/v1/health"),
        ({}, "http://localhost:8080/api/health"),
        ({"WEB_PORT": ""}, "http://localhost:8080/api/health"),
        ({"WEB_PORT": "8181"}, "http://localhost:8181/api/health"),
    ],
)
def test_the_main_images_healthcheck_asks_the_port_the_server_listens_on(env, url):
    assert _url(MAIN, **env) == url


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({}, "http://127.0.0.1:9494/v1/health"),
        ({"NODE_PORT": ""}, "http://127.0.0.1:9494/v1/health"),
        ({"AGENT_PORT": "9600"}, "http://127.0.0.1:9600/v1/health"),
        ({"NODE_PORT": "9500\r"}, "http://127.0.0.1:9500/v1/health"),
        ({"AGENT_PORT": "9600\r"}, "http://127.0.0.1:9600/v1/health"),
    ],
)
def test_the_plex_database_images_healthcheck_asks_the_port_the_node_listens_on(env, url):
    assert _url(PLEX_DB, **env) == url


def test_the_plex_database_image_names_the_only_role_it_carries():
    # The node refuses NODE_ROLES=worker when NODE_IMAGE_ROLES says the image can't run it (nodes/server/app.py).
    env = " ".join(line for line in _instructions(PLEX_DB) if line.startswith("ENV "))
    assert re.search(r"\bNODE_IMAGE_ROLES=plex_db\b", env), env
    assert "NODE_IMAGE_ROLES" not in " ".join(_instructions(MAIN))
