"""nodes.client: a request the node may have run (no answer in time) is told apart from one that never reached it;
answers are bounded and redirects never followed."""

from __future__ import annotations

import gzip
import io
import json
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.nodes import client as client_mod
from media_preview_generator.nodes import protocol
from media_preview_generator.nodes.client import (
    NodeAddress,
    NodeCantStartError,
    NodeClient,
    NodeError,
    NodeProtocolError,
    NodeRequestError,
    NodeStoppingError,
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


class TestEveryFailureIsANodeError:
    def test_an_address_urllib3_refuses_before_connecting_is_unreachable(self):
        # A real session: requests prepares "nas..lan", then urllib3 raises LocationParseError, not a requests error.
        with pytest.raises(NodeUnreachableError):
            NodeClient(NodeAddress("http://nas..lan:9494", "k")).info()

    def test_json_nested_deeper_than_the_parser_goes_is_a_protocol_error(self, serve):
        url, _ = serve(200, b"[" * 100_000 + b"]" * 100_000)
        with pytest.raises(NodeProtocolError):
            _local(url).info()

    @pytest.mark.parametrize("call", ["start", "poll", "info"])
    def test_a_503_nested_deeper_than_the_parser_goes_is_a_node_error(self, call):
        # The 503's body is read to tell a stopping node from a gateway error; its RecursionError ended the file as a
        # crash instead of queueing it again (the executor catches NodeError only).
        session = MagicMock(spec=requests.Session)
        session.request.return_value = _response(503, b"[" * 100_000 + b"]" * 100_000)
        client = NodeClient(NodeAddress("http://node:9494", "k"), session=session)
        with pytest.raises(NodeError):
            if call == "start":
                client.start_task({"op": "x"})
            elif call == "poll":
                client.poll_task("t" * 32)
            else:
                client.info()


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


@pytest.fixture
def self_signed_https(tmp_path):
    """A node behind https with a self-signed certificate: ``(url, certificate file)``."""
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl command")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "1", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )  # fmt: skip
    handler = type("Handler", (_Answer,), {"status": 200, "body": b'{"ok": true}', "headers_out": {}, "seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"https://127.0.0.1:{server.server_port}", str(cert)
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


def _json_of(size: int) -> bytes:
    """A node's JSON answer exactly ``size`` bytes long (padded with whitespace, which JSON allows)."""
    body = json.dumps({"ok": True, "id": "t1", "status": "running", "lines": [], "results": []}).encode()
    return body + b" " * (size - len(body))


class _Streamed:
    """A streamed ``requests`` answer of ``body`` (no socket: a server that doesn't read a POST's body resets it)."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self._body = body

    def iter_content(self, chunk_size: int = 1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]

    def __enter__(self) -> _Streamed:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _answering(status: int, body: bytes) -> NodeClient:
    session = MagicMock(spec=requests.Session)
    session.request.side_effect = lambda *args, **kwargs: _Streamed(status, body)
    return NodeClient(NodeAddress("http://node:9494", "k"), session=session)


_ROUTE_CAPS = [
    pytest.param(lambda c: c.health(), 1, id="health"),
    pytest.param(lambda c: c.info(), 1, id="info"),
    pytest.param(lambda c: c.rescan_gpus(), 1, id="gpu-rescan"),
    pytest.param(lambda c: c.start_task({"op": "x"}), 1, id="start"),
    pytest.param(lambda c: c.cancel_task("t1"), 1, id="cancel"),
    pytest.param(lambda c: c.pause_task("t1"), 1, id="pause"),
    pytest.param(lambda c: c.resume_task("t1"), 1, id="resume"),
    pytest.param(lambda c: c.delete_task("t1"), 1, id="delete"),
    # Up to 2000 log lines with the task's result; the node's last 2000 log lines; 200 files' results.
    pytest.param(lambda c: c.poll_task("t1"), 8, id="poll"),
    pytest.param(lambda c: c.log(), 8, id="log"),
    pytest.param(lambda c: c.access_check([], []), 8, id="access-check"),
]


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

    @pytest.mark.parametrize(("call", "cap_mb"), _ROUTE_CAPS)
    def test_each_route_reads_up_to_its_own_cap_and_refuses_past_it(self, call, cap_mb):
        # JSON parses into ~25x its size, so each route reads only what its answer can need (one 64 MB cap for all of
        # them let a 60 MB /v1/info cost the app 1.4 GB).
        cap = cap_mb * 1024 * 1024
        call(_answering(200, _json_of(cap)))
        with pytest.raises(NodeProtocolError, match=f"more than {cap_mb} MB"):
            call(_answering(200, _json_of(cap + 1)))

    def test_an_error_answer_past_the_small_cap_is_not_parsed(self):
        # A 503's body is read only to tell a stopping node apart: past 1 MB it isn't parsed, so it is a plain 503.
        stopping = json.dumps({"ok": False, "error": {"kind": "node_stopping", "message": "stopping"}}).encode()
        with pytest.raises(NodeStoppingError):
            _answering(503, stopping).info()
        with pytest.raises(NodeUnreachableError, match="answered HTTP 503") as raised:
            _answering(503, stopping + b" " * (1024 * 1024)).info()
        assert not isinstance(raised.value, NodeStoppingError)

    def test_an_artifact_is_held_once_not_copied(self, serve):
        import tracemalloc

        size = 32 * 1024 * 1024
        url, _ = serve(200, b"x" * size)
        client = _local(url)
        tracemalloc.start()
        try:
            data = client.fetch_artifact("t1")
            _now, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert len(data) == size
        assert peak < 1.5 * size

    def test_answers_under_the_caps_come_through(self, serve):
        url, _ = serve(200, b"tar bytes")
        assert _local(url).fetch_artifact("t1") == b"tar bytes"
        url, _ = serve(200, b'{"id": "t1", "status": "running"}', {"Content-Type": "application/json"})
        assert _local(url).poll_task("t1")["status"] == "running"


# Run in a child so its peak memory is the answer's alone. The answer is ``size`` bytes of empty lists, JSON's costliest
# shape to parse (each 3-byte ``[],`` becomes a 64-byte list).
_PARSE_COST_CHILD = r"""
import json, resource, sys
from media_preview_generator.nodes.client import NodeAddress, NodeClient, NodeError

route, size = sys.argv[1], int(sys.argv[2])

def peak_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024

class Answer:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    def __init__(self):
        head = b'{"ok": true, "id": "t1", "status": "running", "filler": ['
        tail = b"[]]}"
        self.parts = [head, b"[]," * ((size - len(head) - len(tail)) // 3), tail]
    def iter_content(self, chunk_size=1):
        for part in self.parts:
            for start in range(0, len(part), chunk_size):
                yield part[start:start + chunk_size]
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False

class Session:
    def request(self, *args, **kwargs):
        return Answer()

before = peak_mb()
client = NodeClient(NodeAddress("http://node.lan:9494", "k" * 24), session=Session())
try:
    client.info() if route == "info" else client.poll_task("t1")
    outcome = "parsed"
except NodeError as exc:
    outcome = "refused"
print(outcome, peak_mb() - before)
"""


class TestAnAnswerCannotCostTheAppGigabytes:
    """Whatever answers at a node's address (``/v1/health`` and ``/v1/info`` need no right key) can't make the app
    parse its answer into gigabytes: a 60 MB info answer under the old 64 MB cap took the app from 118 to 1558 MiB."""

    @pytest.mark.parametrize(
        ("route", "size", "outcome"),
        [
            pytest.param("info", 60_000_000, "refused", id="info-60MB"),
            pytest.param("poll", 60_000_000, "refused", id="poll-60MB"),
            pytest.param("poll", 8 * 1024 * 1024 - 3, "parsed", id="poll-largest-accepted"),
        ],
    )
    def test_the_parse_stays_under_512_mb(self, route, size, outcome):
        done = subprocess.run(
            [sys.executable, "-c", _PARSE_COST_CHILD, route, str(size)],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=300,
            check=True,
        )
        seen, grew_mb = done.stdout.split()[0], int(done.stdout.split()[-1])
        assert seen == outcome
        assert grew_mb < 512, f"{route} {seen} {size} bytes and the peak memory grew by {grew_mb} MB"


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


class TestARefusedStart:
    def _start(self, serve, kind: str) -> None:
        body = {"ok": False, "error": {"kind": kind, "message": "The node's temp folder /node-tmp is full (ENOSPC)"}}
        url, _ = serve(400, json.dumps(body).encode(), {"Content-Type": "application/json"})
        _local(url).start_task({"op": "preview.frames", "path": "/a"})

    def test_a_node_that_cant_take_the_task_is_told_apart_from_a_malformed_request(self, serve):
        # 400 `failed` is the node's temp folder (nothing ran, the file can go elsewhere); 400 `request` is a bug.
        with pytest.raises(NodeCantStartError, match="temp folder /node-tmp is full"):
            self._start(serve, "failed")
        with pytest.raises(NodeRequestError):
            self._start(serve, "request")

    def test_a_node_that_is_stopping_is_told_apart_from_another_503(self, serve):
        body = {"ok": False, "error": {"kind": "node_stopping", "message": "This node is stopping"}}
        url, _ = serve(503, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with pytest.raises(NodeStoppingError) as raised:
            _local(url).start_task({"op": "preview.frames", "path": "/a"})
        assert str(raised.value) == f"{url} is restarting" and isinstance(raised.value, NodeUnreachableError)

        url, _ = serve(503, b"<html>Service Unavailable</html>", {"Content-Type": "text/html"})
        with pytest.raises(NodeUnreachableError) as raised:
            _local(url).start_task({"op": "preview.frames", "path": "/a"})
        assert not isinstance(raised.value, NodeStoppingError)


class TestWhyNothingAnswered:
    """The error says why in plain words (never a Python class name) and carries a code Add node words."""

    def test_a_closed_port_is_connection_refused(self):
        with socket.create_server(("127.0.0.1", 0)) as closed:
            port = closed.getsockname()[1]
        with pytest.raises(NodeUnreachableError) as raised:
            _local(f"http://127.0.0.1:{port}").info()
        assert raised.value.reason == client_mod.UNREACHABLE_REFUSED
        assert str(raised.value) == f"http://127.0.0.1:{port} didn't answer (connection refused)"

    def test_a_name_that_does_not_resolve_says_so(self):
        with pytest.raises(NodeUnreachableError) as raised:
            _local("http://no-such-node.invalid:9494").info()
        assert raised.value.reason == client_mod.UNREACHABLE_DNS
        assert "no machine called no-such-node.invalid was found" in str(raised.value)

    def test_https_at_a_plain_http_answer_says_https(self):
        with pytest.raises(NodeUnreachableError) as raised:
            _client(requests.exceptions.SSLError("wrong version number")).info()
        assert raised.value.reason == client_mod.UNREACHABLE_TLS
        assert str(raised.value) == "http://node:9494 didn't answer over https"

    def test_https_with_a_certificate_this_app_does_not_trust_says_so(self, self_signed_https):
        # It does answer over https (a proxy with a self-signed certificate): "use http://" would be wrong.
        url, _ = self_signed_https
        with pytest.raises(NodeUnreachableError) as raised:
            _local(url).health()
        assert raised.value.reason == client_mod.UNREACHABLE_TLS_CERTIFICATE
        assert str(raised.value) == f"{url} answered over https, but its certificate isn't trusted by this app"

    def test_requests_ca_bundle_makes_this_app_trust_it(self, self_signed_https, monkeypatch):
        # What the guide tells a user with their own certificate to do.
        url, cert = self_signed_https
        for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "CURL_CA_BUNDLE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", cert)
        assert NodeClient(NodeAddress(url, "k")).health() == {"ok": True}

    @pytest.mark.parametrize(
        "error", [requests.ReadTimeout("x"), requests.ConnectTimeout("x"), requests.ConnectionError("x")]
    )
    def test_no_message_names_an_exception_class(self, error):
        with pytest.raises(NodeUnreachableError) as raised:
            _client(error).info()
        assert type(error).__name__ not in str(raised.value)


class TestTheNodesOwnWords:
    def test_a_refusal_is_one_line_of_bounded_length(self, serve):
        # Whatever answers at the address writes this text; the app logs it and shows it on the file's row.
        message = "bad spec\n2026-10-15 03:00:00.000 | ERROR    | web.auth:login:88 - forged\u2028" + "x" * 10_000
        body = json.dumps({"ok": False, "error": {"kind": "request", "message": message}}).encode()
        url, _ = serve(400, body, {"Content-Type": "application/json"})

        with pytest.raises(NodeRequestError) as raised:
            _local(url).start_task({"op": "x"})

        text = str(raised.value)
        assert text.startswith("bad spec\\n2026-10-15") and "\\u2028" in text
        assert "\n" not in text and "\u2028" not in text and len(text) < protocol.RELAY_LINE_MAX + 100


class TestSomethingThatIsNotANode:
    def test_a_web_page_is_flagged(self, serve):
        url, _ = serve(404, b"<!doctype html><title>404</title>", {"Content-Type": "text/html; charset=utf-8"})
        with pytest.raises(NodeProtocolError) as raised:
            _local(url).info()
        assert raised.value.web_page is True and raised.value.app_instance == ""

    def test_a_json_404_names_an_older_plex_database_node(self, serve):
        # What an older Plex database node answers for a node route it predates.
        url, _ = serve(404, b'{"error": "not found"}', {"Content-Type": "application/json"})
        with pytest.raises(NodeProtocolError) as raised:
            _local(url).info()
        assert str(raised.value) == f"{url} doesn't know /v1/info (an older Plex database node?)"

    def test_this_apps_web_side_is_named_by_its_instance_header(self, serve):
        url, _ = serve(404, b"<html></html>", {"Content-Type": "text/html", "X-MPG-App-Instance": "abc123"})
        with pytest.raises(NodeProtocolError) as raised:
            _local(url).info()
        assert raised.value.app_instance == "abc123"
