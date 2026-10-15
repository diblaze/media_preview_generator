"""Lab scripts: tearing a lab down never touches the shared lab servers other sessions use."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODES_LAB = Path(__file__).resolve().parent.parent / "docs" / "design" / "remote-nodes" / "lab" / "nodes_lab.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_nodes_lab_down_removes_only_its_own_containers_and_volumes(tmp_path):
    log = tmp_path / "docker.log"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\n')
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}"}

    subprocess.run(["bash", str(NODES_LAB), "down"], env=env, check=True, capture_output=True, timeout=30)

    calls = log.read_text().splitlines()
    assert calls, "down ran no docker command"
    for call in calls:
        verb, *args = call.split()
        if verb == "volume":
            verb, *args = args
        names = [a for a in args if not a.startswith("-")]
        assert verb in ("rm", "stop"), call
        assert names and all(n.startswith(("mlab-nodes-", "mlab_nodes_")) for n in names), call
