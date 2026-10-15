"""Windows entry point of the Plex database node: the app's Plex database role, running as the user Plex runs as.

Plex on Windows keeps its database on the machine's own disk, and SQLite's locks only work between processes on that
machine, so an app elsewhere can only write markers through a node running here. This is that node, built into a
self-contained folder by ``plex-db-node.spec`` and started at logon by ``install.ps1`` (``HKCU\\...\\Run``, no admin).

Its own files (``node.json`` with the node's id and key, ``logs\\node.log``, ``loudness-writes.jsonl``: the undo record
of each loudness write) live in ``%LOCALAPPDATA%\\MediaPreviewGenerator\\node``, which it makes its ``CONFIG_DIR``.
Plex's folder is ``PLEX_CONFIG_DIR`` when set, else the one Plex itself uses for this user
(``winplatform.default_plex_config_dir``). It serves with waitress (gunicorn doesn't run on Windows) in one process:
the role's "one writer at a time" guarantee is a lock inside that process.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from loguru import logger

# Every interface: the app on another machine is the whole point.
DEFAULT_HOST = "0.0.0.0"  # nosec B104
DEFAULT_PORT = 9494
THREADS = 4
# A line bound with this extra goes to the console only (the node's one-time key notice). The same name as
# nodes.server.nodelog.SECRET_EXTRA, written out: the package is imported only after logging is set up.
CONSOLE_ONLY = "node_secret"


def node_config_dir() -> str:
    """``%LOCALAPPDATA%\\MediaPreviewGenerator\\node``: the node's own id, key and log."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(os.path.join("~", "AppData", "Local"))
    return os.path.join(base, "MediaPreviewGenerator", "node")


class _ToLoguru(logging.Handler):
    """waitress logs through the standard library; its lines go to the same sinks as the node's own."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        logger.opt(exception=record.exc_info).log(level, "{}: {}", record.name, record.getMessage())


def _log_to(config_dir: str) -> str:
    """Log to ``<config_dir>\\logs\\node.log`` (and the console when there is one); return the log file's path."""
    log_file = os.path.join(config_dir, "logs", "node.log")
    logger.remove()
    # The windowed build has no console: sys.stderr is None there.
    # diagnose=False: a traceback's variable values could hold the key or a request body.
    if sys.stderr is not None:
        logger.add(sys.stderr, level="INFO", diagnose=False)
    logger.add(
        log_file,
        level="INFO",
        rotation="5 MB",
        retention=3,
        encoding="utf-8",
        diagnose=False,
        filter=lambda record: not record["extra"].get(CONSOLE_ONLY),
    )
    logging.basicConfig(handlers=[_ToLoguru()], level=logging.INFO, force=True)
    return log_file


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Media Preview Generator Plex database node for Windows")
    parser.add_argument("--host", default=os.environ.get("NODE_HOST", DEFAULT_HOST), help="address to listen on")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("NODE_PORT", DEFAULT_PORT)), help="port to listen on"
    )
    parser.add_argument(
        "--config-dir", default=os.environ.get("NODE_CONFIG_DIR") or node_config_dir(), help="the node's own folder"
    )
    parser.add_argument("--plex-config-dir", default=os.environ.get("PLEX_CONFIG_DIR"), help="Plex's data folder")
    parser.add_argument("--self-test", action="store_true", help="check the build has what the role needs, then exit")
    parser.add_argument(
        "--undo-loudness",
        action="store_true",
        help="put back the audio tracks this node's loudness writes changed (stop Plex first), then exit",
    )
    parser.add_argument("--dry-run", action="store_true", help="with --undo-loudness: count, change nothing")
    return parser.parse_args(argv)


def self_test() -> int:
    """Import what the Plex database role runs and read a JPEG the way chapter checks do (the build's excludes).

    Returns:
        0 when everything loads.
    """
    import io

    from PIL import Image

    from media_preview_generator.loudness import undo  # noqa: F401
    from media_preview_generator.markers.publishers import plex_chapters, plex_db  # noqa: F401
    from media_preview_generator.nodes.server import app, winplatform  # noqa: F401

    buf = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buf, format="JPEG")
    with Image.open(io.BytesIO(buf.getvalue())) as image:
        image.load()
        if image.format != "JPEG":
            raise SystemExit(f"read back as {image.format}")
    winplatform.is_local_drive(os.path.abspath(os.sep))
    print("self-test ok")
    return 0


def _plex_folder(args: argparse.Namespace) -> str:
    from media_preview_generator.nodes.server import winplatform

    return args.plex_config_dir or winplatform.default_plex_config_dir()


def undo_loudness(args: argparse.Namespace) -> int:
    """Run the loudness undo (``loudness.undo``) on this node's undo record and the Plex database it wrote.

    The Windows counterpart of running ``python -m media_preview_generator.loudness.undo`` in a node's container.

    Returns:
        The undo's exit code.
    """
    from media_preview_generator.loudness import undo
    from media_preview_generator.loudness.plex_db import write_log_path
    from media_preview_generator.markers.publishers.plex_db import plex_db_path

    log = write_log_path()
    if not os.path.isfile(log):
        print(f"No loudness undo record at {log}: this node hasn't written loudness.", file=sys.stderr)
        return 1
    undo_args = ["--db", plex_db_path(_plex_folder(args)), "--log", log]
    return undo.main([*undo_args, "--dry-run"] if args.dry_run else undo_args)


def main(argv: list[str] | None = None) -> int:
    """Start the node and serve until stopped.

    Args:
        argv: Command-line arguments (default: ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    args = _parse(argv)
    if args.self_test:
        return self_test()
    # The loudness undo record (loudness.plex_db.write_log_path) goes to CONFIG_DIR: the node's own folder here, not
    # the container's /config, which on Windows would be \config on whatever drive the node started from.
    os.environ["CONFIG_DIR"] = args.config_dir
    if args.undo_loudness:
        return undo_loudness(args)
    os.makedirs(args.config_dir, exist_ok=True)
    log_file = _log_to(args.config_dir)

    # Imported after logging is set up, so an import failure in the windowed build lands in the log file.
    from media_preview_generator.nodes.protocol import ROLE_PLEX_DB

    plex_dir = _plex_folder(args)
    os.environ["PLEX_CONFIG_DIR"] = plex_dir
    os.environ["NODE_ROLES"] = ROLE_PLEX_DB
    logger.info(
        "Plex database node starting: Plex folder {}, node folder {}, log {}", plex_dir, args.config_dir, log_file
    )
    if not os.path.isdir(plex_dir):
        logger.warning(
            "Plex's folder {} doesn't exist (yet); the node answers, but can't open Plex's database", plex_dir
        )

    try:
        from media_preview_generator.nodes.server.app import create_node_app
        from media_preview_generator.nodes.server.wsgi import create_node_server

        app = create_node_app(config_dir=args.config_dir, roles=[ROLE_PLEX_DB])
        server = create_node_server(app, host=args.host, port=args.port, threads=THREADS, ident="mpg-plex-db-node")
        logger.info("Listening on http://{}:{} ({} threads)", args.host, args.port, THREADS)
        server.run()
    except OSError as exc:
        # Most often a second copy (the Run entry and a manual start): the port is already taken.
        logger.error("Couldn't listen on {}:{}: {}", args.host, args.port, exc)
        return 1
    except SystemExit as exc:
        logger.error("The node refused to start: {}", exc)
        return 1
    except Exception:
        logger.exception("The node stopped with an error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
