"""Node identity: node.json created once (0600), kept, and the key's precedence (spec §10.1 env)."""

from __future__ import annotations

import json
import os
import stat

import pytest
from loguru import logger

from media_preview_generator.nodes.server import identity, nodelog


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("NODE_KEY", "AGENT_TOKEN", "NODE_NAME", "CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def warnings_seen():
    seen: list[str] = []
    handler = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING", format="{message}")
    yield seen
    logger.remove(handler)


class TestNodeJson:
    def test_created_once_with_mode_0600_when_missing(self, tmp_path):
        me = identity.load_identity(str(tmp_path))

        path = tmp_path / "node.json"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        stored = json.loads(path.read_text())
        assert stored == {"id": me.id, "key": me.key}
        assert len(me.id) == 32 and me.key_generated and me.key_source == "node.json"

    def test_same_id_and_key_on_the_next_start(self, tmp_path):
        first = identity.load_identity(str(tmp_path))
        second = identity.load_identity(str(tmp_path))

        assert (second.id, second.key) == (first.id, first.key)
        assert not second.key_generated

    def test_config_dir_env_is_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "cfg"))

        identity.load_identity()

        assert (tmp_path / "cfg" / "node.json").is_file()

    def test_corrupt_file_refuses_to_start_instead_of_changing_the_id(self, tmp_path):
        (tmp_path / "node.json").write_text("{not json")

        with pytest.raises(SystemExit, match="node.json"):
            identity.load_identity(str(tmp_path))
        assert (tmp_path / "node.json").read_text() == "{not json"

    def test_wrong_shape_refuses_to_start(self, tmp_path):
        (tmp_path / "node.json").write_text('{"id": 3}')

        with pytest.raises(SystemExit):
            identity.load_identity(str(tmp_path))

    def test_repr_never_shows_the_key(self, tmp_path):
        me = identity.load_identity(str(tmp_path))

        assert me.key not in repr(me)


class TestKeyPrecedence:
    def test_argument_beats_env_and_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_KEY", "from-node-key")
        monkeypatch.setenv("AGENT_TOKEN", "from-agent-token")

        me = identity.load_identity(str(tmp_path), key="from-arg")

        assert (me.key, me.key_source) == ("from-arg", "argument")

    def test_node_key_beats_agent_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_KEY", "from-node-key")
        monkeypatch.setenv("AGENT_TOKEN", "from-agent-token")

        assert identity.load_identity(str(tmp_path)).key == "from-node-key"

    def test_agent_token_beats_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_TOKEN", "from-agent-token")

        me = identity.load_identity(str(tmp_path))

        assert me.key == "from-agent-token"
        assert json.loads((tmp_path / "node.json").read_text())["key"] != "from-agent-token"

    def test_blank_env_falls_through_to_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_KEY", "   ")

        me = identity.load_identity(str(tmp_path))

        assert me.key_source == "node.json"

    @pytest.mark.parametrize("bad", ["clé", "tab\there", "bell\x07"])
    def test_non_printable_ascii_key_refuses_to_start(self, tmp_path, monkeypatch, bad):
        monkeypatch.setenv("NODE_KEY", bad)

        with pytest.raises(SystemExit, match="ASCII"):
            identity.load_identity(str(tmp_path))

    @pytest.mark.parametrize("bad", ["has space", "trailing\u00a0nbsp"], ids=["space", "nbsp"])
    def test_a_key_the_app_would_refuse_to_store_refuses_to_start(self, tmp_path, monkeypatch, bad):
        """The app stores keys of characters 33..126 only (``nodes.settings.normalise_key``); a space passes
        ``isprintable`` but no key the app sends could ever match it."""
        monkeypatch.setenv("NODE_KEY", bad)

        with pytest.raises(SystemExit, match="no spaces"):
            identity.load_identity(str(tmp_path))

    def test_every_character_the_app_accepts_is_accepted(self, tmp_path, monkeypatch):
        every = "".join(chr(c) for c in range(33, 127))
        monkeypatch.setenv("NODE_KEY", every)

        assert identity.load_identity(str(tmp_path)).key == every

    def test_non_ascii_argument_refused(self, tmp_path):
        with pytest.raises(SystemExit):
            identity.load_identity(str(tmp_path), key="ключ")


class TestKeyNotice:
    def test_generated_key_announced_once(self, tmp_path, warnings_seen):
        me = identity.load_identity(str(tmp_path))
        identity.load_identity(str(tmp_path))

        notices = [m for m in warnings_seen if me.key in m]
        assert len(notices) == 1
        assert "Add node" in notices[0] and "NODE_KEY" in notices[0]

    def test_no_notice_when_the_key_comes_from_env(self, tmp_path, monkeypatch, warnings_seen):
        monkeypatch.setenv("NODE_KEY", "mine")

        identity.load_identity(str(tmp_path))

        stored_key = json.loads((tmp_path / "node.json").read_text())["key"]
        assert not [m for m in warnings_seen if stored_key in m or "mine" in m]

    def test_notice_never_reaches_the_readable_node_log(self, tmp_path):
        buffer = nodelog.install()

        me = identity.load_identity(str(tmp_path))

        assert not [line for line in buffer.lines(2000) if me.key in line["message"]]


class TestName:
    def test_node_name_env_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_NAME", "gpu-box")

        assert identity.load_identity(str(tmp_path)).name == "gpu-box"

    def test_hostname_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(identity.socket, "gethostname", lambda: "host-1")

        assert identity.load_identity(str(tmp_path)).name == "host-1"
