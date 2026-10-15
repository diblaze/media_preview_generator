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
