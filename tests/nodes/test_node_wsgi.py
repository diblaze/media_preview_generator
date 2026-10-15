"""The node's server process (``python -m media_preview_generator.nodes.server.wsgi``): it starts once or exits with
its reason, keeps answering while connections hold half-sent requests, and stops cleanly on SIGTERM."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

KEY = "wsgi-test-key-0123456789"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _env(tmp_path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_KEY", "AGENT_TOKEN", "NODE_ROLES", "NODE_PORT")}
    # The Plex database role: no GPU detection running in the background of a test.
    plex = tmp_path / "plex"
    plex.mkdir(exist_ok=True)
    env.update(
        CONFIG_DIR=str(tmp_path / "config"),
        NODE_TEMP_DIR=str(tmp_path / "node-tmp"),
        NODE_ROLES="plex_db",
        PLEX_CONFIG_DIR=str(plex),
        NODE_KEY=KEY,
        LOG_FORMAT="pretty",
    )
    env.update(extra)
    return env


def _command(port: int, threads: int = 4) -> list[str]:
    return [sys.executable, "-m", "media_preview_generator.nodes.server.wsgi", "--host", "127.0.0.1", "--port",
            str(port), "--threads", str(threads)]  # fmt: skip


def _health(port: int, timeout: float) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=timeout) as response:  # nosec B310
        return response.status


@pytest.fixture
def node(tmp_path):
    port = _free_port()
    proc = subprocess.Popen(_command(port), cwd=REPO, env=_env(tmp_path), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)  # fmt: skip
    deadline = time.monotonic() + 60
    while True:
        try:
            _health(port, 1)
            break
        except OSError:
            if proc.poll() is not None or time.monotonic() > deadline:
                proc.kill()
                pytest.fail(f"the node never answered: {proc.communicate()[0]}")
            time.sleep(0.1)
    yield proc, port
    if proc.poll() is None:
        proc.kill()
        proc.communicate()


def test_a_node_that_refuses_to_start_exits_once_with_its_reason(tmp_path):
    done = subprocess.run(_command(_free_port()), cwd=REPO, env=_env(tmp_path, NODE_KEY="change-me"),
                          capture_output=True, text=True, timeout=60)  # fmt: skip

    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert output.count("example key from the docs") == 1, output


def test_a_worker_node_whose_temp_folder_cant_be_made_exits_once_without_a_traceback(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    env = _env(tmp_path, NODE_ROLES="worker", NODE_TEMP_DIR=str(blocker / "node-tmp"))

    done = subprocess.run(_command(_free_port()), cwd=REPO, env=env, capture_output=True, text=True, timeout=60)

    assert done.returncode == 1
    output = done.stdout + done.stderr
    assert "Traceback" not in output, output
    assert output.count("The node didn't start: NODE_TEMP_DIR") == 1, output


@pytest.fixture
def node_env(tmp_path, monkeypatch):
    from loguru import logger

    from media_preview_generator.nodes.server import wsgi

    for name in ("NODE_PORT", "NODE_HOST", "LOG_LEVEL", "AGENT_TOKEN", "NODE_ROLES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MODE", "node")  # main() setdefaults it; set here, it is undone after the test
    monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NODE_KEY", KEY)
    monkeypatch.setattr(wsgi, "_setup_logging", lambda: None)
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(f"{m.record['level'].name} {m.record['message']}"), level="INFO")
    yield wsgi, lines
    logger.remove(sink)


@pytest.mark.parametrize("port", ["abc", "9494x", "-1", "0", "65536", "9494 9495", "\u0663"])
def test_a_node_port_that_isnt_a_port_is_one_line_and_exit_1(node_env, monkeypatch, port):
    wsgi, lines = node_env
    monkeypatch.setenv("NODE_PORT", port)

    assert wsgi.main([]) == 1

    assert lines == [f"ERROR The node didn't start: NODE_PORT is {port!r}, which isn't a port number from 1 to 65535"]


def test_a_bad_port_on_the_command_line_is_one_line_and_exit_1(node_env):
    wsgi, lines = node_env

    assert wsgi.main(["--port", "abc"]) == 1

    assert [line for line in lines if "NODE_PORT is 'abc'" in line] == lines and len(lines) == 1, lines


@pytest.mark.parametrize("value", ["9500", " 9500 ", "9500\r", "9500\r\n", "1", "65535"])
def test_a_port_with_spaces_or_a_windows_line_ending_around_it_is_that_port(value):
    from media_preview_generator.nodes.server import wsgi

    assert wsgi._port(value) == int(value)


def test_a_crlf_node_port_serves_on_that_port(tmp_path):
    # An env file saved on Windows: wrapper.sh hands the value on as it is (--port "${NODE_PORT:-9494}").
    port = _free_port()
    command = [*_command(port)[:-4], "--port", f"{port}\r", "--threads", "4"]
    proc = subprocess.Popen(command, cwd=REPO, env=_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                assert _health(port, 1) == 200
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > deadline:
                    proc.kill()
                    pytest.fail(f"the node never answered: {proc.communicate()[0]}")
                time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_half_sent_requests_dont_stop_health_answering(node):
    proc, port = node
    held = []
    try:
        for _ in range(20):  # five times the node's 4 threads, and no key needed to send them
            conn = socket.create_connection(("127.0.0.1", port))
            conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n")
            held.append(conn)
        time.sleep(0.5)

        start = time.monotonic()
        assert _health(port, 5) == 200
        assert time.monotonic() - start < 2
    finally:
        for conn in held:
            conn.close()


def test_sigterm_stops_the_node_cleanly(node):
    proc, port = node

    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=30)

    assert proc.returncode == 0, output
    assert "Stopping (SIGTERM)" in output and "Node stopped" in output
    with pytest.raises(OSError):
        _health(port, 1)


def test_sigterm_stops_the_node_while_connections_are_busy(node):
    proc, port = node
    held = [socket.create_connection(("127.0.0.1", port)) for _ in range(10)]
    try:
        for conn in held:
            conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n")
        for _ in range(20):
            _health(port, 5)  # the server's loop is busy when the signal lands

        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=30)
    finally:
        for conn in held:
            conn.close()

    assert proc.returncode == 0, output


def test_the_port_host_and_log_level_can_come_from_files_like_every_other_setting(tmp_path, monkeypatch):
    # They were read before the FILE__ values were loaded, and both images passed --port from NODE_PORT themselves:
    # a node given FILE__NODE_PORT listened on 9494 whatever its file said, and FILE__LOG_LEVEL changed nothing, both
    # logged as "set from" their file.
    from types import SimpleNamespace

    from loguru import logger

    from media_preview_generator.nodes.server import wsgi

    for name, value in (("port", "9600"), ("host", "127.0.0.1"), ("level", "DEBUG"), ("puid", "99")):
        (tmp_path / name).write_text(value + "\n")
    for name in ("NODE_PORT", "NODE_HOST", "AGENT_PORT", "LOG_LEVEL", "PUID"):
        monkeypatch.setenv(name, "")  # so monkeypatch takes back the values the node sets
    monkeypatch.setenv("FILE__NODE_PORT", str(tmp_path / "port"))
    monkeypatch.setenv("FILE__NODE_HOST", str(tmp_path / "host"))
    monkeypatch.setenv("FILE__LOG_LEVEL", str(tmp_path / "level"))
    monkeypatch.setenv("FILE__PUID", str(tmp_path / "puid"))
    levels = []
    monkeypatch.setattr(wsgi, "_setup_logging", lambda: levels.append(os.environ.get("LOG_LEVEL")))
    built = SimpleNamespace(extensions={wsgi.EXTENSION: SimpleNamespace(close=lambda **_: None)})
    monkeypatch.setattr(wsgi, "create_node_app", lambda: built)
    listened = {}

    def server(app, *, host, port, threads):
        listened.update(host=host, port=port)
        raise OSError("the test stops here")

    monkeypatch.setattr(wsgi, "create_node_server", server)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING", format="{message}")
    try:
        assert wsgi.main(["--threads", "2"]) == 1
    finally:
        logger.remove(sink)

    assert listened == {"host": "127.0.0.1", "port": 9600}
    assert levels == ["", "DEBUG"]  # set up again at the file's level
    # Read by the container before the node starts: never set from a file, and the log says so.
    assert os.environ["PUID"] == ""
    assert any(w.startswith("FILE__PUID is ignored: the container reads PUID before the node starts") for w in warnings)


def test_a_node_listens_on_agent_port_when_node_port_isnt_set(monkeypatch):
    # The Plex database image passed ${NODE_PORT:-${AGENT_PORT:-9494}} itself; the node now reads both.
    from media_preview_generator.nodes.server import wsgi

    monkeypatch.setenv("NODE_PORT", "")
    monkeypatch.setenv("AGENT_PORT", "9601")
    assert wsgi._port(wsgi._parse([]).port) == 9601
    monkeypatch.setenv("NODE_PORT", "9602")
    assert wsgi._port(wsgi._parse([]).port) == 9602


def test_the_node_server_refuses_an_oversized_chunked_body_before_buffering_it():
    import threading

    from media_preview_generator.nodes.server import wsgi

    def app(environ, start_response):
        environ["wsgi.input"].read()
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"read"]

    server = wsgi.create_node_server(app, host="127.0.0.1", port=0, threads=1)
    serving = threading.Thread(target=server.run, daemon=True)
    serving.start()
    try:
        with socket.create_connection(("127.0.0.1", server.effective_port), timeout=10) as sock:
            sock.sendall(b"POST /v1/tasks HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n")
            chunk = b"x" * 65536
            try:
                for _ in range(wsgi.MAX_REQUEST_BODY_BYTES // len(chunk) + 8):
                    sock.sendall(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                sock.sendall(b"0\r\n\r\n")
            except OSError:
                pass  # the server may close the connection as soon as the body is too big
            status = sock.recv(64).split(b"\r\n")[0]
        assert b" 413 " in status, status
    finally:
        server.close()


def test_a_body_the_node_accepts_is_never_written_to_a_temp_file_before_the_app_sees_it(monkeypatch):
    # waitress reads a body whole before the app checks the key, writing one past 512 KB to a temp file as it
    # arrives: under the old 4 MB limit a thousand key-less connections held 4 GB of the node's temp disk.
    import threading

    from waitress import buffers

    from media_preview_generator.nodes.server import wsgi

    spooled = []
    to_temp_file = buffers.OverflowableBuffer._set_large_buffer

    def recorded(self):
        spooled.append(len(self))
        to_temp_file(self)

    monkeypatch.setattr(buffers.OverflowableBuffer, "_set_large_buffer", recorded)

    def app(environ, start_response):
        environ["wsgi.input"].read()
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"read"]

    server = wsgi.create_node_server(app, host="127.0.0.1", port=0, threads=1)
    threading.Thread(target=server.run, daemon=True).start()
    try:
        body = b"x" * (wsgi.MAX_REQUEST_BODY_BYTES - 1)  # the largest it takes: waitress refuses the limit itself
        head = b"POST /v1/tasks HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body)
        with socket.create_connection(("127.0.0.1", server.effective_port), timeout=10) as sock:
            sock.sendall(head + body)
            assert b" 200 " in sock.recv(64)
    finally:
        server.close()
    assert spooled == []


@pytest.fixture
def served(monkeypatch):
    """A node server in this process for a tiny app; yields its port and lets a test shrink its limits first."""
    import threading

    from media_preview_generator.nodes.server import wsgi

    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    servers = []

    def start(**limits):
        for name, value in limits.items():
            monkeypatch.setattr(wsgi, name, value, raising=False)
        server = wsgi.create_node_server(app, host="127.0.0.1", port=0, threads=2)
        threading.Thread(target=server.run, daemon=True).start()
        servers.append(server)
        return server.effective_port

    yield start
    for server in servers:
        server.close()


def _half_sent(port: int, source: str) -> socket.socket:
    conn = socket.create_connection(("127.0.0.1", port), timeout=5, source_address=(source, 0))
    conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n")
    return conn


def _health_from(port: int, source: str) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=5, source_address=(source, 0)) as conn:
        conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        return conn.recv(64).split(b"\r\n")[0]


def test_one_address_cant_hold_every_connection_the_node_takes(served):
    port = served(CONNECTION_LIMIT=20, CONNECTIONS_PER_ADDRESS=5)
    # 127.0.0.2 is another address on the loopback: the flood comes from it, the app's check from 127.0.0.1.
    held = [_half_sent(port, "127.0.0.2") for _ in range(25)]
    try:
        time.sleep(0.5)
        start = time.monotonic()
        assert b" 200 " in _health_from(port, "127.0.0.1")
        assert time.monotonic() - start < 2
    finally:
        for conn in held:
            conn.close()


def test_each_address_in_an_ipv6_64_has_its_own_limit_and_the_64_a_looser_one(served, monkeypatch):
    """A machine holding its limit doesn't lock out the app beside it in its /64; a new address in the /64 for every
    connection is held to the /64's limit."""
    from media_preview_generator.nodes.server import wsgi

    # Loopback has one IPv6 address, so IPv4 loopback addresses stand in for addresses in one /64.
    in_64 = {"127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"}
    monkeypatch.setattr(wsgi, "caller_network", lambda address: "2001:db8:1:2::/64" if address in in_64 else None)
    port = served(CONNECTION_LIMIT=40, CONNECTIONS_PER_ADDRESS=5, CONNECTIONS_PER_NETWORK=8)
    held = [_half_sent(port, "127.0.0.2") for _ in range(5)]
    try:
        time.sleep(0.5)
        assert b" 200 " in _answer_from(port, "127.0.0.3")
        held += [_half_sent(port, "127.0.0.4") for _ in range(3)]
        time.sleep(0.5)
        assert _answer_from(port, "127.0.0.5") == b""
        assert b" 200 " in _answer_from(port, "127.0.0.1")
    finally:
        for conn in held:
            conn.close()


def _half_sent_body(port: int, source: str, size: int) -> socket.socket:
    conn = socket.create_connection(("127.0.0.1", port), timeout=5, source_address=(source, 0))
    conn.sendall(b"POST /v1/health HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % (2 * size) + b"x" * size)
    return conn


def test_request_bodies_the_node_holds_before_any_key_check_have_a_budget(served):
    # waitress holds each request whole in memory before the key is checked: a thousand connections sending bodies
    # near the body limit held half a gigabyte, enough to take down a small node.
    port = served(REQUEST_BYTES_FREE=10_000, REQUEST_BYTES_BUDGET=200_000)
    first = _half_sent_body(port, "127.0.0.2", 120_000)
    try:
        time.sleep(0.5)
        second = _half_sent_body(port, "127.0.0.3", 120_000)
        try:
            assert _closed_by_the_node(second)
        finally:
            second.close()
        assert b" 200 " in _answer_from(port, "127.0.0.1"), "a request within its free bytes is still read"
    finally:
        first.close()
    time.sleep(0.5)
    third = _half_sent_body(port, "127.0.0.3", 120_000)
    try:
        assert not _closed_by_the_node(third), "the budget comes back once the first connection is gone"
    finally:
        third.close()


def test_one_address_cant_hold_the_whole_request_budget(served):
    # One machine sending large bodies on many connections held all 64 MB: every other caller's large request was
    # closed until its bodies timed out.
    port = served(REQUEST_BYTES_FREE=10_000, REQUEST_BYTES_BUDGET=400_000, REQUEST_BYTES_PER_CALLER=100_000)
    first = _half_sent_body(port, "127.0.0.2", 70_000)
    held = [first]
    try:
        time.sleep(0.5)
        second = _half_sent_body(port, "127.0.0.2", 70_000)
        held.append(second)
        assert _closed_by_the_node(second), "its second body takes it past its share"
        other = _half_sent_body(port, "127.0.0.3", 70_000)
        held.append(other)
        assert not _closed_by_the_node(other), "another address still has the rest of the budget"
        first.close()
        time.sleep(0.5)
        again = _half_sent_body(port, "127.0.0.2", 70_000)
        held.append(again)
        assert not _closed_by_the_node(again), "its share comes back once its first connection is gone"
    finally:
        for conn in held:
            conn.close()


def test_a_machine_with_an_address_per_connection_in_its_ipv6_64_has_one_share_of_the_budget(served, monkeypatch):
    from media_preview_generator.nodes.server import wsgi

    in_64 = {"127.0.0.2", "127.0.0.3"}
    monkeypatch.setattr(wsgi, "caller_network", lambda address: "2001:db8:1:2::/64" if address in in_64 else None)
    port = served(REQUEST_BYTES_FREE=10_000, REQUEST_BYTES_BUDGET=400_000, REQUEST_BYTES_PER_CALLER=100_000)
    held = [_half_sent_body(port, "127.0.0.2", 70_000)]
    try:
        time.sleep(0.5)
        held.append(_half_sent_body(port, "127.0.0.3", 70_000))
        assert _closed_by_the_node(held[-1])
        held.append(_half_sent_body(port, "127.0.0.4", 70_000))
        assert not _closed_by_the_node(held[-1])
    finally:
        for conn in held:
            conn.close()


def _keep_alive(port: int, source: str) -> socket.socket:
    """A connection that had its answer and stays open, as each of the app's worker threads keeps one."""
    conn = socket.create_connection(("127.0.0.1", port), timeout=5, source_address=(source, 0))
    conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: x\r\n\r\n")
    assert b" 200 " in conn.recv(4096)
    return conn


def _closed_by_the_node(conn: socket.socket) -> bool:
    conn.settimeout(1.0)
    try:
        while conn.recv(4096):
            pass  # the rest of the last answer
    except TimeoutError:
        return False
    except OSError:
        pass
    return True


def _answer_from(port: int, source: str) -> bytes:
    try:
        return _health_from(port, source)
    except OSError:
        return b""


def test_an_address_at_its_limit_gives_up_an_idle_keep_alive_rather_than_refuse(served):
    # One keep-alive per worker thread running a file here: 110 workers against a 100-connection limit left 10 polls
    # refused every second, and each refused poll handed a file back.
    port = served(CONNECTIONS_PER_ADDRESS=5)
    idle = [_keep_alive(port, "127.0.0.1") for _ in range(5)]
    try:
        assert b" 200 " in _answer_from(port, "127.0.0.1")
        assert sum(_closed_by_the_node(conn) for conn in idle) == 1
    finally:
        for conn in idle:
            conn.close()


def test_an_address_whose_every_connection_is_busy_is_still_refused(served):
    port = served(CONNECTIONS_PER_ADDRESS=5)
    held = [_half_sent(port, "127.0.0.2") for _ in range(5)]
    try:
        time.sleep(0.5)
        assert _answer_from(port, "127.0.0.2") == b""
        assert not any(_closed_by_the_node(conn) for conn in held)
    finally:
        for conn in held:
            conn.close()


def test_one_address_may_hold_a_connection_for_every_worker_one_app_can_give_a_node():
    from media_preview_generator.nodes.server import wsgi
    from media_preview_generator.worker_groups import MAX_CPU_WORKERS, MAX_GPU_WORKERS

    assert wsgi.CONNECTIONS_PER_ADDRESS > MAX_CPU_WORKERS + MAX_GPU_WORKERS
    assert wsgi.CONNECTIONS_PER_ADDRESS <= wsgi.CONNECTIONS_PER_NETWORK
    # And it takes several machines (or /64s) to take every connection the node has, Windows' smaller limit included.
    assert 3 * wsgi.CONNECTIONS_PER_ADDRESS <= wsgi.SELECT_CONNECTION_LIMIT <= wsgi.CONNECTION_LIMIT
    assert 2 * wsgi.CONNECTIONS_PER_NETWORK <= wsgi.SELECT_CONNECTION_LIMIT


def test_a_request_sent_a_line_at_a_time_is_closed_after_the_read_limit(served):
    port = served(REQUEST_READ_S=1)
    started = time.monotonic()
    conn = _half_sent(port, "127.0.0.1")
    conn.settimeout(0.3)
    try:
        # Never quiet for long, so the channel timeout (inactivity only) would keep it open for good.
        while time.monotonic() - started < 10:
            try:
                conn.sendall(b"X-a: b\r\n")
                if conn.recv(1) == b"":
                    break
            except TimeoutError:
                continue  # nothing to read: still open
            except OSError:
                break
    finally:
        conn.close()
    assert time.monotonic() - started < 5
