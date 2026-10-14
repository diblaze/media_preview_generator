"""The fixture CI boots the Plex database node image against answers the calls that job makes, the way it expects.

``.github/workflows/ci.yml`` (``marker-agent-image``) builds the folder with ``scripts/plex_db_node_fixture.py``,
mounts it into the image and calls ``/v1/loudness/check`` and ``/v1/library-health/rows``; this runs the same calls
against the role in-process, so a fixture the role would refuse fails here first.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from media_preview_generator.markers.publishers import plex_remote
from tests.markers.test_plex_db_publisher import _mountinfo
from tests.nodes.plex_db_harness import TOKEN, node_app

AUTH = {"Authorization": f"Bearer {TOKEN}", plex_remote.PROTOCOL_HEADER: str(plex_remote.AGENT_PROTOCOL)}
SCRIPT = Path(__file__).parents[2] / "scripts/plex_db_node_fixture.py"


def _fixture_module():
    spec = importlib.util.spec_from_file_location("plex_db_node_fixture", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _node(tmp_path, monkeypatch):
    fixture = _fixture_module()
    folder = tmp_path / "plex"
    fixture.build(folder)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "node-config"))
    return node_app(str(folder), mountinfo_path=_mountinfo(tmp_path)).test_client(), fixture


def test_the_loudness_check_answers_with_a_report(tmp_path, monkeypatch):
    node, fixture = _node(tmp_path, monkeypatch)
    body = {"machine_identifier": fixture.DEFAULT_MACHINE, "pms_version": "1.43.4.10903-ab12cd34", "deadline_s": 5}
    answer = node.post("/v1/loudness/check", json=body, headers=AUTH)
    assert answer.status_code == 200
    result = answer.get_json()["result"]
    assert result["capability"] == "loudness_v1" and isinstance(result["report"]["state"], str)


def test_library_health_counts_the_one_film(tmp_path, monkeypatch):
    node, fixture = _node(tmp_path, monkeypatch)
    body = {"section_ids": [1], "machine_identifier": fixture.DEFAULT_MACHINE, "deadline_s": 5}
    answer = node.post("/v1/library-health/rows", json=body, headers=AUTH)
    assert answer.status_code == 200
    result = answer.get_json()["result"]
    assert result["capability"] == "library_health_v1"
    assert len(result["rows"]["parts"]) == 1 and result["rows"]["audio_counts"] == [[1, 1]]
