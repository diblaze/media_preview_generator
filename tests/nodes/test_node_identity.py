"""Node identity: node.json created once (0600), kept, and the key's precedence (spec §10.1 env)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap

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
        assert stored == {"id": me.id, "key": me.key, "announced": True}
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

        with pytest.raises(SystemExit, match="node.json") as refused:
            identity.load_identity(str(tmp_path))
        assert (tmp_path / "node.json").read_text() == "{not json"
        assert "gives the node a new id and key, and every app that added it has to add it again" in str(refused.value)

    @pytest.mark.parametrize(
        "stored",
        ['{"id": 3}', '{"id": "", "key": "k"}', '{"id": "  ", "key": "k"}'],
        ids=["not-text", "blank-id", "spaces-id"],
    )
    def test_wrong_shape_refuses_to_start(self, tmp_path, stored):
        # A blank id is no id: the app could never save one for the node, so nothing would check which machine
        # answers at its address.
        (tmp_path / "node.json").write_text(stored)

        with pytest.raises(SystemExit, match="isn't a node identity file; restore it from a backup"):
            identity.load_identity(str(tmp_path))

    @pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any file")
    def test_a_node_json_made_under_another_puid_names_both_users_and_doesnt_suggest_deleting_it(
        self, tmp_path, monkeypatch
    ):
        # A PUID/PGID change left node.json (0600) to the old user, and the advice to remove it re-keyed the node.
        identity.load_identity(str(tmp_path))
        path = tmp_path / "node.json"
        owner = os.stat(path).st_uid
        path.chmod(0)
        monkeypatch.setattr(identity.os, "geteuid", lambda: 99)
        try:
            with pytest.raises(SystemExit) as refused:
                identity.load_identity(str(tmp_path))
        finally:
            path.chmod(0o600)

        message = str(refused.value)
        assert f"it belongs to user {owner} and this node runs as user 99" in message
        assert "Set PUID/PGID back to the user that made it" in message and "remove" not in message

    def test_repr_never_shows_the_key(self, tmp_path):
        me = identity.load_identity(str(tmp_path))

        assert me.key not in repr(me)


class TestKeyPrecedence:
    def test_argument_beats_env_and_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_KEY", "from-node-key-0123456789")
        monkeypatch.setenv("AGENT_TOKEN", "from-agent-token")

        me = identity.load_identity(str(tmp_path), key="from-arg")

        assert (me.key, me.key_source) == ("from-arg", "argument")

    def test_node_key_beats_agent_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_KEY", "from-node-key-0123456789")
        monkeypatch.setenv("AGENT_TOKEN", "from-agent-token")

        assert identity.load_identity(str(tmp_path)).key == "from-node-key-0123456789"

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


class TestPublicOrWeakKeys:
    @pytest.mark.parametrize("sample", ["change-me-to-a-long-random-key", "change-me", "CHANGE-ME-please-0123456789"])
    @pytest.mark.parametrize("variable", ["NODE_KEY", "AGENT_TOKEN"])
    def test_a_docs_example_key_refuses_to_start(self, tmp_path, monkeypatch, variable, sample):
        monkeypatch.setenv(variable, sample)

        with pytest.raises(SystemExit, match="example key from the docs") as refused:
            identity.load_identity(str(tmp_path))
        assert "openssl rand -hex 24" in str(refused.value)

    def test_a_docs_example_key_as_the_argument_refuses_to_start(self, tmp_path):
        with pytest.raises(SystemExit, match="example key"):
            identity.load_identity(str(tmp_path), key="change-me")

    @pytest.mark.parametrize("short", ["x", "abc123", "fifteen-chars-x"])
    def test_a_short_node_key_refuses_to_start(self, tmp_path, monkeypatch, short):
        monkeypatch.setenv("NODE_KEY", short)

        with pytest.raises(SystemExit, match=f"too short to be safe \\({len(short)} characters; use at least 16\\)"):
            identity.load_identity(str(tmp_path))

    @pytest.mark.parametrize("variable", ["NODE_KEY", "AGENT_TOKEN"])
    def test_a_key_longer_than_the_app_takes_refuses_to_start(self, tmp_path, monkeypatch, variable):
        # The app refuses to store a key over 200 characters, so a node started with one could never be added.
        monkeypatch.setenv(variable, "k" * 201)

        with pytest.raises(SystemExit, match=r"too long \(201 characters; the app takes at most 200\)"):
            identity.load_identity(str(tmp_path))

    def test_a_node_key_of_200_characters_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_KEY", "k" * 200)

        assert identity.load_identity(str(tmp_path)).key == "k" * 200

    def test_a_node_key_of_16_characters_is_kept(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NODE_KEY", "sixteen-chars-ok")

        assert identity.load_identity(str(tmp_path)).key == "sixteen-chars-ok"

    def test_a_plex_helpers_short_agent_token_keeps_working_with_a_warning(self, tmp_path, monkeypatch, warnings_seen):
        monkeypatch.setenv("AGENT_TOKEN", "old-token")

        me = identity.load_identity(str(tmp_path))

        assert (me.key, me.key_source) == ("old-token", "AGENT_TOKEN")
        assert [m for m in warnings_seen if "AGENT_TOKEN is only 9 characters" in m and "NODE_KEY" in m]
        assert not [m for m in warnings_seen if "old-token" in m]


class TestKeyNotice:
    def test_generated_key_announced_once(self, tmp_path, warnings_seen):
        me = identity.load_identity(str(tmp_path))
        identity.load_identity(str(tmp_path))

        notices = [m for m in warnings_seen if me.key in m]
        assert len(notices) == 1
        assert "Add node" in notices[0] and "NODE_KEY" in notices[0]

    def test_no_notice_when_the_key_comes_from_env(self, tmp_path, monkeypatch, warnings_seen):
        monkeypatch.setenv("NODE_KEY", "mine-0123456789abcdef")

        identity.load_identity(str(tmp_path))

        stored_key = json.loads((tmp_path / "node.json").read_text())["key"]
        assert not [m for m in warnings_seen if stored_key in m or "mine-0123456789abcdef" in m]

    def test_a_stored_key_is_announced_the_first_time_it_is_used(self, tmp_path, monkeypatch, warnings_seen):
        monkeypatch.setenv("NODE_KEY", "a-key-the-user-chose-0123456789")
        identity.load_identity(str(tmp_path))  # first start (or install.ps1) with NODE_KEY: node.json made, unused
        monkeypatch.delenv("NODE_KEY")

        later = identity.load_identity(str(tmp_path))  # the next start without it runs on node.json's key
        identity.load_identity(str(tmp_path))

        assert later.key_source == "node.json"
        assert len([m for m in warnings_seen if later.key in m]) == 1
        assert json.loads((tmp_path / "node.json").read_text())["announced"] is True

    def test_a_node_json_from_before_the_flag_is_announced_once(self, tmp_path, warnings_seen):
        (tmp_path / "node.json").write_text(json.dumps({"id": "a" * 32, "key": "k" * 43}))

        identity.load_identity(str(tmp_path))
        identity.load_identity(str(tmp_path))

        assert len([m for m in warnings_seen if "k" * 43 in m]) == 1

    def test_a_read_only_node_json_still_starts_and_shows_the_key_again(self, tmp_path, monkeypatch, warnings_seen):
        identity.load_identity(str(tmp_path), key="from-arg")  # made, unused

        def read_only(*_args):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(identity, "_write_private", read_only)

        first = identity.load_identity(str(tmp_path))
        second = identity.load_identity(str(tmp_path))

        assert first.key == second.key
        assert len([m for m in warnings_seen if first.key in m]) == 2
        assert [m for m in warnings_seen if "shown again next start" in m]

    def test_notice_reaches_docker_logs_but_never_app_log(self, tmp_path):
        """A worker node runs the app's setup_logging (the full image): its app.log is mode 0644 and goes into bug
        reports, beside the 0600 node.json meant to be the key's only home. A process of its own, so this one's
        handlers stay as they are."""
        script = textwrap.dedent(
            """
            import os
            from loguru import logger
            from media_preview_generator.logging_config import setup_logging
            from media_preview_generator.nodes.server import identity

            setup_logging(log_level="INFO")
            identity.load_identity(os.environ["CONFIG_DIR"])
            logger.complete()
            """
        )
        env = {k: v for k, v in os.environ.items() if k not in ("NODE_KEY", "AGENT_TOKEN")}
        env.update(CONFIG_DIR=str(tmp_path), LOG_FORMAT="pretty")

        done = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=60)

        assert done.returncode == 0, done.stderr
        key = json.loads((tmp_path / "node.json").read_text())["key"]
        assert key in done.stderr
        app_log = (tmp_path / "logs" / "app.log").read_text()
        assert key not in app_log and "node.json" not in app_log

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


class TestCallerKey:
    @pytest.mark.parametrize(
        ("address", "key", "network"),
        [
            ("10.0.0.66", "10.0.0.66", None),
            ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
            ("2001:0db8:0001:0002::0001", "2001:db8:1:2::1", "2001:db8:1:2::/64"),
            ("fd12:3456:789a:1::5", "fd12:3456:789a:1::5", "fd12:3456:789a:1::/64"),
            ("fe80::1", "fe80::1", "fe80::/64"),
            ("::ffff:10.0.0.66", "10.0.0.66", None),
            ("::1", "::1", "::/64"),
            ("", "", None),
            ("not-an-address", "not-an-address", None),
        ],
        ids=["ipv4", "ipv6", "ipv6-long-form", "ula", "link-local", "ipv4-mapped", "loopback", "empty", "not-ip"],
    )
    def test_each_address_is_a_caller_and_an_ipv6_ones_64_is_counted_too(self, address, key, network):
        assert identity.caller_key(address) == key
        assert identity.caller_network(address) == network


class TestWrongKeyLimit:
    def _limit(self):
        now = [0.0]
        return identity.WrongKeyLimit(burst=3, refill_s=10.0, clock=lambda: now[0]), now

    def test_an_address_is_blocked_after_its_burst_and_gets_one_more_try_per_refill(self):
        limit, now = self._limit()
        for _ in range(3):
            assert limit.blocked_for("10.0.0.66") == 0
            limit.note_wrong("10.0.0.66")
        assert limit.blocked_for("10.0.0.66") == 10
        now[0] = 5.0
        assert limit.blocked_for("10.0.0.66") == 5
        now[0] = 10.0
        assert limit.blocked_for("10.0.0.66") == 0
        limit.note_wrong("10.0.0.66")
        assert limit.blocked_for("10.0.0.66") == 10

    def test_other_addresses_and_an_address_that_stopped_guessing_are_not_blocked(self):
        limit, now = self._limit()
        for _ in range(3):
            limit.note_wrong("10.0.0.66")
        assert limit.blocked_for("10.0.0.5") == 0
        now[0] = 30.0
        assert limit.blocked_for("10.0.0.66") == 0

    def test_an_address_held_back_is_named_in_the_log_once_per_run_of_wrong_keys(self):
        limit, now = self._limit()
        lines: list[str] = []
        sink = identity.logger.add(lambda message: lines.append(str(message)), level="WARNING")
        try:
            for _ in range(10):  # an old copy that keeps sending its old key
                limit.note_wrong("10.0.0.66")
                now[0] += 1.0
            now[0] += 100.0  # it stopped long enough to owe nothing...
            for _ in range(3):  # ...then started again
                limit.note_wrong("10.0.0.66")
        finally:
            identity.logger.remove(sink)
        named = [line for line in lines if "10.0.0.66" in line]
        assert len(named) == 2 and "wait, even with the right key" in named[0]

    @pytest.mark.parametrize(
        "prefix", ["2001:db8:1:2::", "fd12:3456:789a:1::", "fe80::"], ids=["isp", "ula", "link-local"]
    )
    def test_a_device_with_an_old_key_holds_back_only_itself_not_the_rest_of_its_64(self, prefix):
        # Every machine of a home network shares its /64: one old copy with an old key held back the app beside it.
        limit, _now = self._limit()
        for _ in range(3):
            limit.note_wrong(f"{prefix}66")
        assert limit.blocked_for(f"{prefix}66") > 0
        assert limit.blocked_for(f"{prefix}5") == 0

    def test_a_new_ipv6_address_for_every_guess_is_held_back_after_ten_addresses_worth(self):
        limit, _now = self._limit()
        lines: list[str] = []
        sink = identity.logger.add(lambda message: lines.append(str(message)), level="WARNING")
        try:
            for n in range(3 * identity.WRONG_KEY_NETWORK_FACTOR):
                assert limit.blocked_for("2001:db8:1:2:ffff::9") == 0
                limit.note_wrong(f"2001:db8:1:2::{n + 1:x}")
        finally:
            identity.logger.remove(sink)
        assert limit.blocked_for("2001:db8:1:2:ffff::9") > 0
        assert limit.blocked_for("2001:db8:1:3::1") == 0
        assert [line for line in lines if "addresses in 2001:db8:1:2::/64" in line]

        for n in range(3):
            limit.note_wrong(f"10.0.0.{n + 1}")
        assert limit.blocked_for("10.0.0.4") == 0

    def test_wrong_keys_from_other_addresses_never_free_one_held_back(self, monkeypatch):
        monkeypatch.setattr(identity, "WRONG_KEY_ADDRESSES_MAX", 10)
        limit, now = self._limit()
        for _ in range(3):
            limit.note_wrong("10.0.0.66")
        now[0] = 1.0
        limit.note_wrong("10.0.0.67")  # owing, not held back: forgotten before any newer one
        for n in range(20):  # an attacker cycling addresses to push 10.0.0.66 out of the table
            now[0] = 2.0 + n * 0.01
            limit.note_wrong(f"10.0.1.{n}")

        assert limit.blocked_for("10.0.0.66") > 0
        assert "10.0.0.67" not in limit._owed and len(limit._owed) <= 10

    def test_it_remembers_a_bounded_number_of_addresses(self, monkeypatch):
        monkeypatch.setattr(identity, "WRONG_KEY_ADDRESSES_MAX", 10)
        limit, _now = self._limit()
        for n in range(50):
            limit.note_wrong(f"10.0.1.{n}")
        assert len(limit._owed) <= 10
