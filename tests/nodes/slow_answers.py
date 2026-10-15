"""A listener that answers slowly: its headers at once, then a byte at a time (JSON allows leading spaces)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def dripping_server(body: bytes, *, drip_s: float, gap_s: float, sent_at_once: int = 0) -> Iterator[str]:
    """Answer every request with a 200 whose body is a space every ``gap_s`` for ``drip_s``, then ``body``.

    Args:
        body: What the answer ends with.
        drip_s: How long the spaces go on.
        gap_s: The time between two spaces: far under any read timeout, so no single read times out.
        sent_at_once: How many answers go out whole at once before the dripping starts.

    Yields:
        The listener's URL.
    """
    server = socket.create_server(("127.0.0.1", 0))
    answered = [0]

    def handle(conn: socket.socket) -> None:
        with conn:
            received = b""
            try:
                while b"\r\n\r\n" not in received:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    received += chunk
                spaces = 0 if answered[0] < sent_at_once else round(drip_s / gap_s)
                answered[0] += 1
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {spaces + len(body)}\r\nConnection: close\r\n\r\n".encode()
                )
                for _ in range(spaces):
                    conn.sendall(b" ")
                    time.sleep(gap_s)
                conn.sendall(body)
            except OSError:
                return  # the app hung up

    def serve() -> None:
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
        try:
            server.shutdown(socket.SHUT_RDWR)  # wakes the accept() waiting in serve()
        except OSError:
            pass
        server.close()
