"""Serve a node with waitress: ``python -m media_preview_generator.nodes.server.wsgi`` (``MODE=node`` in wrapper.sh,
and the Plex database image).

One process: the task table lives in its memory, so a second process would answer polls for tasks it never started,
and the Plex database role's "one writer at a time" is a lock inside it. waitress reads each request whole in its own
event loop before a thread takes it, so connections that send a request slowly, or never finish one, hold no thread:
``/v1/health`` and the app's polls keep answering. Nothing here needs the key, so anyone who can reach the port could
otherwise freeze the node with a few half-sent requests. Nor can they hold every connection the node takes: one
address gets :data:`CONNECTIONS_PER_ADDRESS` at once and an IPv6 /64 :data:`CONNECTIONS_PER_NETWORK` (past that, one of
their idle keep-alives makes room), and each request must arrive whole within :data:`REQUEST_READ_S`. Nor fill its
memory: requests being read hold :data:`REQUEST_BYTES_BUDGET` at most beyond their first :data:`REQUEST_BYTES_FREE`, and
one caller's :data:`REQUEST_BYTES_PER_CALLER` of it.
"""

from __future__ import annotations

import argparse
import logging
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
from .app import CONTAINER_SETTINGS, EXTENSION, FILE_ENV_PREFIX, SERVER_THREADS, _env_from_files, create_node_app
from .identity import caller_key, caller_network

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
# Connections one address may hold at once. Each of the app's workers running a file here keeps one open (a keep-alive
# per worker thread), and one app can give a node up to MAX_CPU_WORKERS + MAX_GPU_WORKERS of them, plus its checks,
# clean-ups and pages (a few dozen at most). An address at the limit only loses an idle keep-alive, which it opens again,
# so this needn't be more; it leaves room for six such machines (three on Windows) before the node is full (waitress has
# no per-address limit of its own).
CONNECTIONS_PER_ADDRESS = MAX_CPU_WORKERS + MAX_GPU_WORKERS + 32
# Connections one IPv6 /64 may hold at once (``identity.caller_network``): a machine there can take a new address for
# every connection, and every other machine of its network shares the /64. Half of Windows' smaller limit.
CONNECTIONS_PER_NETWORK = SELECT_CONNECTION_LIMIT // 2
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
# How much of an answer may wait for a slow app before the thread sending it waits too (waitress's own default, named
# here for OUTBUF_OVERFLOW_BYTES).
OUTBUF_HIGH_WATERMARK_BYTES = 16 * 1024 * 1024
# waitress moves what waits past this to a temp file (its default is 1 MB), and a Plex database node may run with
# --read-only, without one: Library Health's answer (~1 KB a file) went past it whenever the app read slower than the
# node sent, and the answer stopped partway. Twice the high watermark, so the waiting part plus one more piece of an
# answer (a few MB at most; an artifact is sent from its file) stays in memory.
OUTBUF_OVERFLOW_BYTES = 2 * OUTBUF_HIGH_WATERMARK_BYTES
# waitress reads each request whole in memory before the app checks its key, so a thousand connections sending bodies
# near MAX_REQUEST_BODY_BYTES would hold half a gigabyte of a small node's memory. The bytes of requests being read (or
# waiting for a thread) beyond the first REQUEST_BYTES_FREE of each count against REQUEST_BYTES_BUDGET; a connection
# whose request would take more than is left is closed. The app's requests are tens of KB, so they only meet the budget
# while something else holds it.
REQUEST_BYTES_FREE = 64 * 1024
REQUEST_BYTES_BUDGET = 64 * 1024 * 1024
# One caller's share of it, so one machine sending large bodies on many connections can't leave every other caller's
# large request closed. A caller is an address, or an IPv6 /64 as a whole: a machine there can take an address per
# connection.
REQUEST_BYTES_PER_CALLER = REQUEST_BYTES_BUDGET // 4
# How long a stopping node waits for its cancelled tasks (and their FFmpeg) to stop, inside Docker's 10 s stop timeout;
# in the image s6 kills the node sooner, 3 s after the SIGTERM (see tasks.STOP_TELL_WAIT_S).
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
    """A waitress ``channel_class`` that holds an address to :data:`CONNECTIONS_PER_ADDRESS` connections (an IPv6 /64
    to :data:`CONNECTIONS_PER_NETWORK`), closes one whose request hasn't arrived whole within :data:`REQUEST_READ_S`,
    and one whose request would take more of :data:`REQUEST_BYTES_BUDGET` than is left, or its caller past
    :data:`REQUEST_BYTES_PER_CALLER`.

    At a limit, one of the address's (or the /64's) idle keep-alives is closed to make room, and only one whose every
    connection is busy (reading, running or answering a request) is refused: a client keeping more keep-alives than
    the limit then only loses idle ones, which it opens again. The one closed is the one most recently busy: the
    app's workers poll on a timer, so its owner is the furthest from sending on it again.

    Args:
        listen_map: The server's socket map: every open connection is in it.

    Returns:
        What the server calls for each accepted connection.
    """
    from waitress.channel import HTTPChannel

    # Request bytes counted against the budget now (waitress's loop thread changes it; its service threads don't).
    budget_used = [0]
    # The same bytes by caller (an address, or an IPv6 /64): only callers holding some are in it.
    caller_used: dict[str, int] = {}
    warned_at: dict[str, float] = {}

    def warn(what: str, message: str, *args: Any) -> None:
        now = time.monotonic()
        if now - warned_at.get(what, -REFUSED_WARNING_S - 1) > REFUSED_WARNING_S:
            warned_at[what] = now
            logger.warning(message, *args)

    class NodeChannel(HTTPChannel):
        reading_since: float | None = None
        will_close: bool  # waitress sets it; its package has no types
        caller = ""
        network: str | None = None
        held_bytes = 0

        def _charge(self) -> int:
            return max(0, self.held_bytes - REQUEST_BYTES_FREE)

        def _sender(self) -> str:
            return self.network or self.caller

        def _release(self) -> None:
            charge = self._charge()
            if charge:
                budget_used[0] -= charge
                left = caller_used.pop(self._sender(), 0) - charge
                if left > 0:
                    caller_used[self._sender()] = left
            self.held_bytes = 0

        def received(self, data: bytes) -> bool:
            if not data or self.will_close or self.close_when_flushed:
                return super().received(data)  # nothing it keeps
            if self.request is None and not self.requests:
                self._release()  # the last request was answered: its bytes are gone
            before = self._charge()
            self.held_bytes += len(data)
            added = self._charge() - before
            full = budget_used[0] + added > REQUEST_BYTES_BUDGET
            if full or caller_used.get(self._sender(), 0) + added > REQUEST_BYTES_PER_CALLER:
                self.held_bytes -= len(data)
                self._release()
                self.will_close = True
                if full:
                    warn(
                        "budget",
                        "Closed a connection from {}: requests being read already hold {} MB of the node's memory",
                        self.caller,
                        REQUEST_BYTES_BUDGET >> 20,
                    )
                else:
                    warn(
                        "caller budget",
                        "Closed a connection from {}: requests being read from there already hold {} MB of the node's "
                        "memory, the most one caller may",
                        f"addresses in {self.network}" if self.network else self.caller,
                        REQUEST_BYTES_PER_CALLER >> 20,
                    )
                return False
            if added:
                budget_used[0] += added
                caller_used[self._sender()] = caller_used.get(self._sender(), 0) + added
            if self.request is None:
                self.reading_since = time.monotonic()
            done = super().received(data)
            if self.request is None:
                self.reading_since = None
            return done

        def readable(self) -> bool:
            # waitress asks every connection this on each pass of its loop (at least once a second).
            if self.held_bytes and self.request is None and not self.requests:
                self._release()
            if self.reading_since is not None and time.monotonic() - self.reading_since > REQUEST_READ_S:
                self.will_close = True
            return super().readable()

        def del_channel(self, map: dict | None = None) -> None:
            self._release()
            super().del_channel(map)

    def room(held: list[NodeChannel], limit: int) -> bool:
        """Whether one more connection fits under ``limit``, closing the most recently busy idle one of ``held``
        (removed from it) when they are at it."""
        if len(held) >= limit:
            quiet = next((d for d in sorted(held, key=lambda d: -d.last_activity) if _quiet(d)), None)
            if quiet is not None:
                # waitress's loop calls this, so the channel is closed here and now (it leaves the map at once).
                quiet.handle_close()
                held.remove(quiet)
        return len(held) < limit

    def accept(server: Any, sock: Any, addr: Any, adj: Any, map: dict | None = None) -> NodeChannel | None:
        address = addr[0] if isinstance(addr, tuple) else str(addr)
        peer, network = caller_key(address), caller_network(address)
        open_now = [d for d in list(listen_map.values()) if isinstance(d, NodeChannel)]
        refused: tuple[str, int] | None = None
        if not room([d for d in open_now if d.caller == peer], CONNECTIONS_PER_ADDRESS):
            refused = peer, CONNECTIONS_PER_ADDRESS
        elif network is not None:
            # ``connected``: one closed just above to make room is still in ``open_now``.
            same_network = [d for d in open_now if d.network == network and d.connected]
            if not room(same_network, CONNECTIONS_PER_NETWORK):
                refused = f"addresses in {network}", CONNECTIONS_PER_NETWORK
        if refused is None:
            channel = NodeChannel(server, sock, addr, adj, map=map)
            channel.caller, channel.network = peer, network
            return channel
        sock.close()
        warn("connections", "Refused connections from {}: {} are open from there to this node already", *refused)
        return None

    return accept


class QueueDepthWarnings(logging.Filter):
    """Lets waitress's "Task queue depth" warning through at most once every :data:`REFUSED_WARNING_S`; the next one let
    through says how many were held back since the last.

    waitress warns once per request that arrives with every thread taken, and an app's workers send a few dozen at once:
    thousands of lines for a queue that empties in moments.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._warned_at = -REFUSED_WARNING_S - 1
        self._held_back = 0
        self._deepest = 0

    def filter(self, record: logging.LogRecord) -> bool:
        depth = record.args[0] if isinstance(record.args, tuple) and record.args else 0
        with self._lock:
            self._deepest = max(self._deepest, depth if isinstance(depth, int) else 0)
            now = time.monotonic()
            if now - self._warned_at <= REFUSED_WARNING_S:
                self._held_back += 1
                return False
            if self._held_back:
                record.msg = "Task queue depth is %d (%d more such warnings since the last one, deepest %d)"
                record.args = (depth, self._held_back, self._deepest)
            self._warned_at, self._held_back, self._deepest = now, 0, 0
            return True


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

    queue_logger = logging.getLogger("waitress.queue")
    if not any(isinstance(f, QueueDepthWarnings) for f in queue_logger.filters):
        queue_logger.addFilter(QueueDepthWarnings())
    config = getattr(app, "config", None)
    if isinstance(config, dict):
        # The Plex database role leaves some of them free for the requests that never wait on Plex (plex_db.py).
        config[SERVER_THREADS] = threads
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
        outbuf_high_watermark=OUTBUF_HIGH_WATERMARK_BYTES,
        outbuf_overflow=OUTBUF_OVERFLOW_BYTES,
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
    # AGENT_PORT: the Plex marker agent's name for it, still read when NODE_PORT isn't set. Checked in _serve, once
    # logging is up: a bad NODE_PORT is one logged line, not argparse's usage or a traceback.
    port = os.environ.get("NODE_PORT", "").strip() or os.environ.get("AGENT_PORT", "").strip() or str(DEFAULT_PORT)
    parser.add_argument("--port", default=port)
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS, help="requests served at once")
    return parser.parse_args(argv)


def _port(value: str) -> int | None:
    """``value`` as a port to listen on, or None when it isn't one.

    Args:
        value: NODE_PORT or ``--port``; spaces and a Windows line ending (an env file saved with CRLF) around it are
            ignored.

    Returns:
        The port (1-65535), or None.
    """
    text = value.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def _host_port(host: str, port: int) -> str:
    """``host:port`` as a URL writes it: an IPv6 address in brackets (``[::]:9494``, not ``:::9494``)."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


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
        return _serve(argv)
    finally:
        logger.complete()  # the console sinks are queued: their last lines must reach docker logs before the exit


def _logging_settings() -> tuple[str | None, str | None]:
    return os.environ.get("LOG_LEVEL"), os.environ.get("LOG_FORMAT")


def _serve(argv: list[str] | None) -> int:
    # Logging first: setup_logging's first call removes every handler, and create_node_app adds the node's own.
    _setup_logging()
    # The FILE__ values before any setting is read here (the log level, port and host), so each works like the rest.
    logging_was = _logging_settings()
    try:
        _env_from_files()
    except SystemExit as exc:
        logger.error("The node didn't start: {}", exc)
        return 1
    if _logging_settings() != logging_was:
        _setup_logging()
    for name in CONTAINER_SETTINGS:
        if os.environ.get(FILE_ENV_PREFIX + name):
            logger.warning(
                "{}{} is ignored: the container reads {} before the node starts. Set {} itself.",
                FILE_ENV_PREFIX,
                name,
                name,
                name,
            )
    args = _parse(argv)
    port = _port(args.port)
    if port is None:
        logger.error("The node didn't start: NODE_PORT is {!r}, which isn't a port number from 1 to 65535", args.port)
        return 1
    args.port = port
    try:
        app = create_node_app()
    except SystemExit as exc:
        logger.error("The node didn't start: {}", exc)
        return 1
    state = app.extensions[EXTENSION]

    try:
        server = create_node_server(app, host=args.host, port=args.port, threads=args.threads)
    except OSError as exc:
        logger.error("The node couldn't listen on {}: {}", _host_port(args.host, args.port), exc)
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
    logger.info("Listening on http://{} ({} threads)", _host_port(args.host, args.port), args.threads)
    serving = threading.Thread(target=serve, name="node-server", daemon=True)
    serving.start()
    while not stopping and serving.is_alive():
        serving.join(STOP_POLL_S)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    logger.info("Stopping ({}): cancelling running tasks", stopping[0] if stopping else "the server ended")
    # Tasks first: their FFmpeg must stop even if Docker kills the process soon after.
    state.close(wait_s=SHUTDOWN_WAIT_S)
    # Still serving, so the app hears the tasks were stopped by a restart rather than finding them gone afterwards.
    state.tasks.wait_until_told()
    server.close()
    logger.info("Node stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
