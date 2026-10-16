"""The Plex database node image installs the app with ``--no-deps`` (nodes/plex-db/requirements.txt only).

Anything its modules import from the rest of the app must not pull in the preview pipeline's dependencies, or the
node dies at start (a markers probe importing ``processing`` once took ``config`` and ``dotenv`` with it).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

# The app's own dependencies that nodes/plex-db/requirements.txt leaves out.
NOT_IN_NODE_IMAGE = (
    "dotenv",
    "plexapi",
    "numpy",
    "cv2",
    "pymediainfo",
    "apscheduler",
    "sqlalchemy",
    "flask_socketio",
    "socketio",
    "engineio",
    "gunicorn",
    "onnxruntime",
    "flask_limiter",
    "flask_wtf",
    "flask_cors",
    "rich",
    "shapely",
    "pyclipper",
)

NODE_MODULES = (
    "media_preview_generator.nodes.server.wsgi",
    "media_preview_generator.nodes.server.app",
    "media_preview_generator.nodes.server.plex_db",
)


def test_the_plex_database_node_imports_without_the_apps_other_dependencies() -> None:
    script = textwrap.dedent(
        f"""
        import importlib, importlib.abc, sys

        class Blocked(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {NOT_IN_NODE_IMAGE!r}:
                    raise ModuleNotFoundError(f"No module named {{name!r}} (not in the node image)")
                return None

        sys.meta_path.insert(0, Blocked())
        for module in {NODE_MODULES!r}:
            importlib.import_module(module)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
