"""``NodeClient`` is shared by the health loop and every job thread: each thread gets its own HTTP session."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import requests

from media_preview_generator.nodes.client import NodeAddress, NodeClient


def _session_seen_by(client: NodeClient) -> requests.Session:
    seen: list[requests.Session] = []
    thread = threading.Thread(target=lambda: seen.append(client._session))
    thread.start()
    thread.join()
    return seen[0]


def test_each_thread_gets_its_own_session_and_keeps_it():
    client = NodeClient(NodeAddress("http://nuc:9494", "k"))
    mine = client._session
    assert client._session is mine
    other = _session_seen_by(client)
    assert isinstance(other, requests.Session) and other is not mine


def test_requests_from_two_threads_go_through_two_sessions(monkeypatch):
    used: list[int] = []

    def request(self, method, url, **kwargs):
        used.append(id(self))
        response = MagicMock(status_code=200, content=b"{}")
        response.json.return_value = {"ok": True}
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    client = NodeClient(NodeAddress("http://nuc:9494", "k"))
    client.health()
    worker = threading.Thread(target=client.health)
    worker.start()
    worker.join()
    assert len(used) == 2 and used[0] != used[1]


def test_an_injected_session_is_used_by_every_thread():
    session = MagicMock(spec=requests.Session)
    client = NodeClient(NodeAddress("http://nuc:9494", "k"), session=session)
    assert client._session is session
    assert _session_seen_by(client) is session
