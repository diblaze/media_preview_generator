"""A job's cancel must not wait out a healthy-but-slow frames download (up to ARTIFACT_DEADLINE_S = 15 minutes)."""

from __future__ import annotations

import contextlib
import io
import socket
import tarfile
import threading
import time

import pytest

from media_preview_generator.nodes import executor, protocol
from media_preview_generator.nodes.client import (
    UNREACHABLE_TIMEOUT,
    DownloadCancelledError,
    NodeAddress,
    NodeClient,
    unreachable_advice,
)
from media_preview_generator.nodes.executor import NodeTaskCancelledError, Placement, placed_on, run_on_node
from media_preview_generator.nodes.registry import set_registry
from tests.nodes.executor_fakes import NODE_ID, NODE_NAME, FakeRegistry, ScriptedClient, finished


def _tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("00001.jpg")
        info.size = 4
        tf.addfile(info, io.BytesIO(b"abcd"))
    return buf.getvalue()


@contextlib.contextmanager
def slow_artifact_server(tar: bytes, *, pieces: int, gap_s: float):
    """Answers a 200 whose body is the tar, then zero padding (tar's end marker) sent 512 bytes every gap_s."""
    server = socket.create_server(("127.0.0.1", 0))

    def handle(conn):
        with conn:
            try:
                received = b""
                while b"\r\n\r\n" not in received:
                    received += conn.recv(65536)
                total = len(tar) + 512 * pieces
                conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {total}\r\nConnection: close\r\n\r\n".encode() + tar)
                for _ in range(pieces):
                    conn.sendall(b"\0" * 512)
                    time.sleep(gap_s)
            except OSError:
                return

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        server.close()


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.0)
    yield
    set_registry(None)


def test_cancel_during_a_slow_but_healthy_frames_download_ends_the_file_promptly(tmp_path):
    media = tmp_path / "S01E01.mkv"
    media.write_bytes(b"0123456789")
    cancelled = []
    with slow_artifact_server(_tar(), pieces=200, gap_s=0.05) as url:  # bytes keep coming: never a stalled link
        real = NodeClient(NodeAddress(url, "k"))
        client = ScriptedClient([finished(protocol.STATUS_DONE, result={})])

        def fetch(task_id, cancel_check=None):
            cancelled.append(True)  # the user cancels the job while the frames are downloading
            return real.fetch_artifact(task_id, cancel_check=cancel_check)

        client.fetch_artifact = fetch
        set_registry(FakeRegistry({NODE_ID: client}))
        started = time.monotonic()
        with pytest.raises(NodeTaskCancelledError):
            with placed_on(Placement(NODE_ID, NODE_NAME)):
                run_on_node(
                    protocol.OP_INTRO_FINGERPRINT, str(media), {"duration_ms": 1000},
                    gpu=None, cancel_check=lambda: bool(cancelled), artifact_to=str(tmp_path / "out"),
                )  # fmt: skip
        assert time.monotonic() - started < 4.0


def test_client_stops_a_flowing_download_when_the_cancel_check_turns_true():
    with slow_artifact_server(_tar(), pieces=200, gap_s=0.05) as url:
        started = time.monotonic()
        with pytest.raises(DownloadCancelledError):
            NodeClient(NodeAddress(url, "k")).fetch_artifact("t1", cancel_check=lambda: True)
        assert time.monotonic() - started < 2.0


def test_a_stall_after_the_headers_over_https_does_not_say_use_http():
    advice = unreachable_advice(UNREACHABLE_TIMEOUT, "https://nuc:9494", "It", connected=True, stalled=True)
    assert "http://" not in advice and "partway" in advice
