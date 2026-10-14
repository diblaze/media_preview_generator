"""Gunicorn entry point for a node (``MODE=node`` in wrapper.sh).

Run with ``--workers 1``: the task table lives in this process's memory, so a second worker would answer polls for
tasks it never started.
"""

from __future__ import annotations

import os
import sys

from loguru import logger

from .app import create_node_app


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


# Logging first: setup_logging's first call removes every handler, and create_node_app adds the node's own.
_setup_logging()
app = create_node_app()
