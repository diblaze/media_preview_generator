"""Serve a node with waitress: ``python -m media_preview_generator.nodes.server.wsgi`` (``MODE=node`` in wrapper.sh,
and the Plex database image).

One process: the task table lives in its memory, so a second process would answer polls for tasks it never started,
and the Plex database role's "one writer at a time" is a lock inside it. waitress reads each request whole in its own
event loop before a thread takes it, so connections that send a request slowly, or never finish one, hold no thread:
``/v1/health`` and the app's polls keep answering. Nothing here needs the key, so anyone who can reach the port could
otherwise freeze the node with a few half-sent requests.
"""

from __future__ import annotations

import argparse
import os
import select
import signal
import sys
import threading
from types import FrameType
from typing import Any

from loguru import logger

from .app import EXTENSION, MAX_BODY_BYTES, create_node_app

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
# Above the routes' own 2 MB limit, so they answer an oversized body in the node's JSON; anything far beyond it is
# refused before waitress buffers it.
MAX_REQUEST_BODY_BYTES = 2 * MAX_BODY_BYTES
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

    return create_server(
        app,
        host=host,
        port=port,
        threads=threads,
        channel_timeout=CHANNEL_TIMEOUT_S,
        connection_limit=CONNECTION_LIMIT if hasattr(select, "poll") else SELECT_CONNECTION_LIMIT,
        asyncore_use_poll=True,
        max_request_body_size=MAX_REQUEST_BODY_BYTES,
        ident=ident,
    )


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
