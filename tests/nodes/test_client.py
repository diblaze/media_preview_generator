"""nodes.client: a request the node may have run (no answer in time) is told apart from one that never reached it;
answers are bounded and redirects never followed."""

from __future__ import annotations

import gzip
import io
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.nodes import client as client_mod
from media_preview_generator.nodes.client import (
    NodeAddress,
    NodeClient,
    NodeProtocolError,
    NodeTaskDeletedError,
    NodeTimeoutError,
    NodeUnreachableError,
)


def _client(error: Exception) -> NodeClient:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = error
    return NodeClient(NodeAddress("http://node:9494", "secret-key"), session=session)


class TestTimeouts:
    def test_no_answer_after_the_request_was_sent_is_a_timeout(self):
        with pytest.raises(NodeTimeoutError) as raised:
            _client(requests.ReadTimeout("read timed out")).start_task({"op": "x"})
        assert isinstance(raised.value, NodeUnreachableError)  # callers that only know "unreachable" still catch it
        assert "secret-key" not in str(raised.value)

    @pytest.mark.parametrize(
        "error",
        [requests.ConnectTimeout("connect timed out"), requests.ConnectionError("refused")],
        ids=["connect-timeout", "refused"],
    )
    def test_a_request_that_never_reached_the_node_is_not_a_timeout(self, error):
        with pytest.raises(NodeUnreachableError) as raised:
            _client(error).start_task({"op": "x"})
        assert not isinstance(raised.value, NodeTimeoutError)


def _node_that_drops_the_connection(*, reset: bool) -> tuple[str, threading.Thread]:
    """A listener that reads one request and then closes the connection without answering: plainly (the client sees
    RemoteDisconnected) or with an RST (ConnectionResetError)."""
    server = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                received += chunk
            if reset:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{server.getsockname()[1]}", thread


class TestAConnectionDroppedMidAnswer:
    """The request went out and the node may have acted on it, exactly as when its answer times out."""

    @pytest.mark.parametrize("reset", [False, True], ids=["closed", "reset"])
    def test_is_a_timeout(self, reset):
        url, thread = _node_that_drops_the_connection(reset=reset)
        with pytest.raises(NodeTimeoutError) as raised:
            NodeClient(NodeAddress(url, "secret-key")).start_task({"op": "x"})
        thread.join(timeout=5)
        assert "secret-key" not in str(raised.value)

    def test_a_connection_refused_is_still_not_a_timeout(self):
        with socket.create_server(("127.0.0.1", 0)) as closed:
            port = closed.getsockname()[1]
        with pytest.raises(NodeUnreachableError) as raised:
            NodeClient(NodeAddress(f"http://127.0.0.1:{port}", "k")).start_task({"op": "x"})
        assert not isinstance(raised.value, NodeTimeoutError)


def _response(status: int, body: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body
    response._content_consumed = True
    return response


class TestAStartAfterItsDelete:
    def test_is_its_own_error(self):
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _response(
            409, b'{"ok": false, "error": {"kind": "task_deleted", "message": "deleted"}}'
        )
        client = NodeClient(NodeAddress("http://node:9494", "k"), session=session)
        with pytest.raises(NodeTaskDeletedError, match="deleted"):
            client.start_task({"op": "x"})


class _Answer(BaseHTTPRequestHandler):
    """Answers every request with the class's ``status``, ``headers_out`` and ``body``; records the paths asked."""

    status = 200
    headers_out: dict[str, str] = {}
    body = b""
    seen: list[str] = []

    def _answer(self) -> None:
        type(self).seen.append(self.path)
        self.send_response(self.status)
        for name, value in self.headers_out.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    do_GET = do_POST = do_DELETE = _answer

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def serve():
    servers: list[ThreadingHTTPServer] = []

    def start(status: int, body: bytes, headers: dict[str, str] | None = None) -> tuple[str, type[_Answer]]:
        attrs = {"status": status, "body": body, "headers_out": headers or {}, "seen": []}
        handler = type("Handler", (_Answer,), attrs)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", handler

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _gzipped_zeros(size: int) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as gz:
        gz.write(b"\0" * size)
    return buf.getvalue()


def _local(url: str) -> NodeClient:
    session = requests.Session()
    session.trust_env = False  # a proxy from the environment must not sit between the test and 127.0.0.1
    return NodeClient(NodeAddress(url, "k"), session=session)


class TestAnswersAreBounded:
    """A node (or whatever answers at its address) can't make the app hold more than a preview's worth of bytes."""

    def test_a_compressed_artifact_is_refused_not_inflated(self, serve):
        # 16 MiB of zeros in ~16 KB on the wire: requests would inflate it whatever the download cap.
        url, _ = serve(200, _gzipped_zeros(16 * 1024 * 1024), {"Content-Encoding": "gzip"})
        with pytest.raises(NodeProtocolError, match="gzip-compressed"):
            _local(url).fetch_artifact("t1")

    def test_a_compressed_json_answer_is_refused(self, serve):
        url, _ = serve(200, _gzipped_zeros(1024), {"Content-Encoding": "gzip", "Content-Type": "application/json"})
        with pytest.raises(NodeProtocolError, match="gzip-compressed"):
            _local(url).poll_task("t1")

    def test_an_artifact_past_the_cap_is_refused(self, serve, monkeypatch):
        monkeypatch.setattr(client_mod, "MAX_ARTIFACT_BYTES", 1024 * 1024)
        url, _ = serve(200, b"x" * (3 * 1024 * 1024))
        with pytest.raises(NodeProtocolError, match="more than 1 MB"):
            _local(url).fetch_artifact("t1")

    def test_a_json_answer_past_the_cap_is_refused(self, serve, monkeypatch):
        monkeypatch.setattr(client_mod, "MAX_ANSWER_BYTES", 1024 * 1024)
        url, _ = serve(200, b'{"pad": "' + b"x" * (3 * 1024 * 1024) + b'"}')
        with pytest.raises(NodeProtocolError, match="more than 1 MB"):
            _local(url).poll_task("t1")

    def test_answers_under_the_caps_come_through(self, serve):
        url, _ = serve(200, b"tar bytes")
        assert _local(url).fetch_artifact("t1") == b"tar bytes"
        url, _ = serve(200, b'{"id": "t1", "status": "running"}', {"Content-Type": "application/json"})
        assert _local(url).poll_task("t1")["status"] == "running"


class TestRedirects:
    def test_a_redirect_is_an_error_and_the_task_never_reaches_the_other_host(self, serve):
        other, other_handler = serve(200, b'{"id": "x"}', {"Content-Type": "application/json"})
        node, _ = serve(307, b"", {"Location": f"{other}/v1/tasks"})
        with pytest.raises(NodeProtocolError, match="redirect"):
            _local(node).start_task({"op": "x", "path": "/media/a.mkv"})
        assert other_handler.seen == []


class TestAProtocolRefusal:
    def _client(self, serve, body: dict) -> NodeClient:
        url, _ = serve(409, json.dumps(body).encode(), {"Content-Type": "application/json"})
        return _local(url)

    def test_from_a_node_carries_its_node_block(self, serve):
        node = {"id": "u1", "name": "nuc", "version": "9.10.0", "protocols": [2], "roles": ["worker"]}
        body = {"ok": False, "node": node, "error": {"kind": "protocol", "message": "speaks 2"}}
        with pytest.raises(NodeProtocolError) as raised:
            self._client(serve, body).info()
        assert raised.value.node == node

    def test_from_an_older_plex_helper_carries_none(self, serve):
        body = {"ok": False, "agent": {"version": "1.2.0"}, "error": {"kind": "protocol", "message": "speaks 1"}}
        with pytest.raises(NodeProtocolError) as raised:
            self._client(serve, body).info()
        assert raised.value.node is None
