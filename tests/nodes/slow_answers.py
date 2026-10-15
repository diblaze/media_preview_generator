"""Listeners that answer slowly: a body a byte at a time (JSON allows leading spaces), or the headers."""

from __future__ import annotations

import socket
import ssl
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


def answering_late(*, wait_s: float, pieces: list[bytes], gap_s: float = 0.0) -> str:
    """A listener that answers one GET only after ``wait_s`` (a busy node), then sends ``pieces`` ``gap_s`` apart."""
    server = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            received = b""
            while b"\r\n\r\n" not in received:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                received += chunk
            time.sleep(wait_s)
            length = sum(len(piece) for piece in pieces)
            head = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n\r\n"
            try:
                conn.sendall(head.encode())
                for piece in pieces:
                    conn.sendall(piece)
                    time.sleep(gap_s)
            except OSError:
                pass  # the app gave up
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return f"http://127.0.0.1:{server.getsockname()[1]}"


@contextmanager
def headers_dripping_server(
    body: bytes, *, drip_s: float, gap_s: float, tls: ssl.SSLContext | None = None, dripped: int | None = None
) -> Iterator[str]:
    """Answer every request with a 200 whose status line and headers come a byte every ``gap_s`` for ``drip_s``.

    Args:
        body: The body sent once the headers are done.
        drip_s: How long the status line and headers take to arrive.
        gap_s: The time between two bytes: far under any read timeout, so no single read times out.
        tls: Serve https with this context.
        dripped: How many answers drip before the rest go out whole at once (None: all of them).

    Yields:
        The listener's URL.
    """
    server = socket.create_server(("127.0.0.1", 0))
    answered = [0]

    def handle(conn: socket.socket) -> None:
        try:
            if tls is not None:
                conn = tls.wrap_socket(conn, server_side=True)
            with conn:
                received = b""
                while b"\r\n\r\n" not in received:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    received += chunk
                pad = b"a" * max(1, round(drip_s / gap_s) - 40)
                head = b"HTTP/1.1 200 OK\r\nX-Pad: " + pad + b"\r\nContent-Type: application/json\r\n"
                drips = dripped is None or answered[0] < dripped
                answered[0] += 1
                if drips:
                    for byte in head:
                        conn.sendall(bytes([byte]))
                        time.sleep(gap_s)
                else:
                    conn.sendall(head)
                conn.sendall(f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
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
        yield f"{'https' if tls is not None else 'http'}://127.0.0.1:{server.getsockname()[1]}"
    finally:
        try:
            server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        server.close()
