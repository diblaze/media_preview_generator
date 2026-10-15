"""nodes.client: a request the node may have run (no answer in time) is told apart from one that never reached it."""

from __future__ import annotations

import socket
import struct
import threading
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.nodes.client import (
    NodeAddress,
    NodeClient,
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


class TestAStartAfterItsDelete:
    def test_is_its_own_error(self):
        response = MagicMock(status_code=409)
        response.json.return_value = {"ok": False, "error": {"kind": "task_deleted", "message": "deleted"}}
        session = MagicMock(spec=requests.Session)
        session.request.return_value = response
        client = NodeClient(NodeAddress("http://node:9494", "k"), session=session)
        with pytest.raises(NodeTaskDeletedError, match="deleted"):
            client.start_task({"op": "x"})
