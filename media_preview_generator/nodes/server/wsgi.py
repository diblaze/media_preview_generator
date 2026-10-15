"""Serve a node with waitress: ``python -m media_preview_generator.nodes.server.wsgi`` (``MODE=node`` in wrapper.sh,
and the Plex database image).

One process: the task table lives in its memory, so a second process would answer polls for tasks it never started,
and the Plex database role's "one writer at a time" is a lock inside it. waitress reads each request whole in its own
event loop before a thread takes it, so connections that send a request slowly, or never finish one, hold no thread:
``/v1/health`` and the app's polls keep answering. Nothing here needs the key, so anyone who can reach the port could
otherwise freeze the node with a few half-sent requests. Nor can they hold every connection the node takes: one
address gets :data:`CONNECTIONS_PER_ADDRESS` at once (past that, one of its idle keep-alives makes room), and each
request must arrive whole within :data:`REQUEST_READ_S`.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import sys
import threading
import time
from types import FrameType
from typing import Any

from loguru import logger

from ...worker_groups import MAX_CPU_WORKERS, MAX_GPU_WORKERS
from .app import EXTENSION, create_node_app
from .identity import caller_key

# Every interface: the app on another machine is the whole point.
DEFAULT_HOST = "0.0.0.0"  # nosec B104
DEFAULT_PORT = 9494
DEFAULT_THREADS = 16
# A connection with no whole request for this long is closed: one sending a request slowly can't keep its slot.
CHANNEL_TIMEOUT_S = 60
# Far above what the apps that use a node keep open (a few per running task), and what bounds a flood of idle
# connections; poll() instead of select() so it isn't capped at 1024 file descriptors.
CONNECTION_LIMIT = 1000
# Windows has no poll(), and its select() takes at most 512 sockets: room is left for the listener and waitress's own.
SELECT_CONNECTION_LIMIT = 500
# Connections one address (an IPv6 caller's whole /64: ``identity.caller_key``) may hold at once. Each of the app's
# workers running a file here keeps one open (a keep-alive per worker thread), and one app can give a node up to
# MAX_CPU_WORKERS + MAX_GPU_WORKERS of them, plus a few for its checks and clean-ups. Twice that leaves room for both
# and for a second app on the same machine, and stays under SELECT_CONNECTION_LIMIT, so no single machine can take
# every connection the node has (waitress has no per-address limit of its own).
CONNECTIONS_PER_ADDRESS = 2 * (MAX_CPU_WORKERS + MAX_GPU_WORKERS)
# A request (headers and body) must arrive whole within this long of its first byte. CHANNEL_TIMEOUT_S only closes a
# connection that went quiet, so one header line every few seconds would otherwise hold a connection forever.
REQUEST_READ_S = 30
# A node refusing connections says so at most this often.
REFUSED_WARNING_S = 60.0
# waitress reads a request's whole body before the app (and its key check) sees it, so anyone who can reach the port
# makes the node hold this much per connection. The app's largest requests are tens of KB (a Test access batch with
# its mapping rows, a film's chapter rows), far under it; the routes' own 2 MB limit is never reached through here.
MAX_REQUEST_BODY_BYTES = 512 * 1024
# Bodies up to this size stay in memory; waitress writes a larger one to a temp file while it arrives (its default is
# 512 KB: 4 MB bodies from a thousand connections held 4 GB of the node's temp disk for REQUEST_READ_S). Twice the
# body limit, as a chunked body is cut off up to one read past it: no body is ever written out.
INBUF_OVERFLOW_BYTES = 2 * MAX_REQUEST_BODY_BYTES
# How long a stopping node waits for its cancelled tasks (and their FFmpeg) to stop: inside Docker's 10 s stop timeout.
SHUTDOWN_WAIT_S = 5.0
STOP_POLL_S = 0.2


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    try:
        from ...logging_config import setup_logging
    except ImportError:
        # The slim Plex database builds carry no Rich; plain stderr is all their docker logs need.
        logger.remove()
        logger.add(sys.stderr, level=level, diagnose=False)
        return
    setup_logging(log_level=level)


def _quiet(channel: Any) -> bool:
    """Whether a connection sits open with nothing on it: no request being read, run or answered, and none arriving.

    Args:
        channel: A waitress ``HTTPChannel``.

    Returns:
        True when closing it loses nothing (a client's keep-alive between requests, or one its peer closed).
    """
    if channel.requests or channel.request is not None or channel.total_outbufs_len:
        return False
    if channel.will_close or channel.close_when_flushed:
        return False
    try:
        # A request already on its way, not yet read: closing now would drop it.
        waiting = channel.socket.recv(1, socket.MSG_PEEK)
    except BlockingIOError:
        return True
    except OSError:
        return True  # a broken connection: nothing to lose either
    return not waiting


def _limited_channels(listen_map: dict) -> Any:
    """A waitress ``channel_class`` that holds an address to :data:`CONNECTIONS_PER_ADDRESS` connections and closes one
    whose request hasn't arrived whole within :data:`REQUEST_READ_S`.

    At the limit, one of the address's idle keep-alives is closed to make room, and only an address whose every
    connection is busy (reading, running or answering a request) is refused: a client keeping more keep-alives than
    the limit then only loses idle ones, which it opens again. The one closed is the one most recently busy: the
    app's workers poll on a timer, so its owner is the furthest from sending on it again.

    Args:
        listen_map: The server's socket map: every open connection is in it.

    Returns:
        What the server calls for each accepted connection.
    """
    from waitress.channel import HTTPChannel

    class NodeChannel(HTTPChannel):
        reading_since: float | None = None
        caller = ""

        def received(self, data: bytes) -> bool:
            if self.request is None and data:
                self.reading_since = time.monotonic()
            done = super().received(data)
            if self.request is None:
                self.reading_since = None
            return done

        def readable(self) -> bool:
            # waitress asks every connection this on each pass of its loop (at least once a second).
            if self.reading_since is not None and time.monotonic() - self.reading_since > REQUEST_READ_S:
                self.will_close = True
            return super().readable()

    warned_at: list[float] = []

    def accept(server: Any, sock: Any, addr: Any, adj: Any, map: dict | None = None) -> NodeChannel | None:
        peer = caller_key(addr[0] if isinstance(addr, tuple) else str(addr))
        held = [d for d in list(listen_map.values()) if isinstance(d, NodeChannel) and d.caller == peer]
        if len(held) >= CONNECTIONS_PER_ADDRESS:
            quiet = next((d for d in sorted(held, key=lambda d: -d.last_activity) if _quiet(d)), None)
            if quiet is not None:
                # waitress's loop calls this, so the channel is closed here and now (it leaves the map at once).
                quiet.handle_close()
                held.remove(quiet)
        if len(held) < CONNECTIONS_PER_ADDRESS:
            channel = NodeChannel(server, sock, addr, adj, map=map)
            channel.caller = peer
            return channel
        sock.close()
        now = time.monotonic()
        if not warned_at or now - warned_at[0] > REFUSED_WARNING_S:
            warned_at[:] = [now]
            logger.warning(
                "Refused connections from {}: it already holds {} open to this node", peer, CONNECTIONS_PER_ADDRESS
            )
        return None

    return accept


def create_node_server(app: Any, *, host: str, port: int, threads: int, ident: str = "mpg-node") -> Any:
    """The waitress server for a node app, with the limits that keep slow or oversized requests from tying it up.

    Both entry points use it: this module and the Windows Plex database node.

    Args:
        app: The node's WSGI app.
        host: The interface to listen on.
        port: The port to listen on.
        threads: How many requests are served at once.
        ident: The ``Server`` header.

    Returns:
        The server, listening; ``run()`` serves until it is closed.

    Raises:
        OSError: When it can't listen there (most often: the port is taken).
    """
    from waitress import create_server

    listen_map: dict = {}
    server = create_server(
        app,
        map=listen_map,
        host=host,
        port=port,
        threads=threads,
        channel_timeout=CHANNEL_TIMEOUT_S,
        connection_limit=CONNECTION_LIMIT if hasattr(select, "poll") else SELECT_CONNECTION_LIMIT,
        asyncore_use_poll=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        inbuf_overflow=INBUF_OVERFLOW_BYTES,
        ident=ident,
    )
    accept = _limited_channels(listen_map)
    # One listener per address it listens on (a host name can resolve to both IPv4 and IPv6), all in the one map.
    for dispatcher in list(listen_map.values()):
        if hasattr(dispatcher, "channel_class"):
            dispatcher.channel_class = accept
    return server


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Media Preview Generator node")
    parser.add_argument("--host", default=os.environ.get("NODE_HOST", "").strip() or DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=int(os.environ.get("NODE_PORT", "").strip() or DEFAULT_PORT))
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS, help="requests served at once")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Start the node and serve until SIGTERM or SIGINT.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).

    Returns:
        The exit code: 1 when the node refused to start (the reason is logged once) or couldn't listen.
    """
    # wrapper.sh sets it in the image; a node started some other way is one too: its log hints name the app's screens
    # for it, and its FFmpeg dies with it (utils.is_node_mode).
    os.environ.setdefault("MODE", "node")
    try:
        return _serve(_parse(argv))
    finally:
        logger.complete()  # the console sinks are queued: their last lines must reach docker logs before the exit


def _serve(args: argparse.Namespace) -> int:
    # Logging first: setup_logging's first call removes every handler, and create_node_app adds the node's own.
    _setup_logging()
    try:
        app = create_node_app()
    except SystemExit as exc:
        logger.error("The node didn't start: {}", exc)
        return 1
    state = app.extensions[EXTENSION]

    try:
        server = create_node_server(app, host=args.host, port=args.port, threads=args.threads)
    except OSError as exc:
        logger.error("The node couldn't listen on {}:{}: {}", args.host, args.port, exc)
        state.close()
        return 1
    # waitress's loop runs in a thread of its own and the main thread waits for SIGTERM/SIGINT, so the stop below can
    # cancel the tasks before closing the server. The handler only appends to a list: it takes no lock, so it is safe
    # whatever the main thread is doing when the signal lands.
    stopping: list[str] = []

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        stopping.append(signal.Signals(signum).name)

    def serve() -> None:
        try:
            server.run()
        except Exception:  # noqa: BLE001 - the node must still stop its tasks below
            logger.opt(exception=True).error("The node's server stopped with an error")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    logger.info("Listening on http://{}:{} ({} threads)", args.host, args.port, args.threads)
    serving = threading.Thread(target=serve, name="node-server", daemon=True)
    serving.start()
    while not stopping and serving.is_alive():
        serving.join(STOP_POLL_S)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    logger.info("Stopping ({}): cancelling running tasks", stopping[0] if stopping else "the server ended")
    # Tasks first: their FFmpeg must stop even if Docker kills the process soon after.
    state.close(wait_s=SHUTDOWN_WAIT_S)
    server.close()
    logger.info("Node stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
