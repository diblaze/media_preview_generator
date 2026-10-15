# PyInstaller spec for the Windows Plex database node. Build with build.ps1 (from the repository root):
#   powershell -File nodes/windows/build.ps1
# Two programs share one folder (onedir): PlexDbNode.exe, windowed, which the Startup entry runs, and
# PlexDbNode-debug.exe, the same code with a console, for watching it start by hand.
# -*- mode: python ; coding: utf-8 -*-

import os

HERE = SPECPATH  # noqa: F821 - PyInstaller defines it: the folder this spec is in

# The Plex database role is the markers publishers and the node server; everything below is the worker role or the
# app's own web UI, never imported with roles=["plex_db"] but reachable through lazy imports PyInstaller would follow.
EXCLUDES = [
    "media_preview_generator.web",
    "media_preview_generator.gpu",
    "media_preview_generator.processing",
    "media_preview_generator.jobs",
    "media_preview_generator.nodes.server.ops",
    "numpy",
    "cv2",
    "onnxruntime",
    "sqlalchemy",
    "apscheduler",
    "plexapi",
    "flask_socketio",
    "pymediainfo",
    "tkinter",
    "gunicorn",
    # Build tooling some library imports optionally; nothing here runs it.
    "setuptools",
    "pkg_resources",
    "_distutils_hack",
    # Pillow only checks that chapter images are JPEGs: its AVIF (7.6 MB), WebP and colour-profile parts stay out.
    "PIL._avif",
    "PIL.AvifImagePlugin",
    "PIL._webp",
    "PIL.WebPImagePlugin",
    "PIL._imagingcms",
    "PIL.ImageCms",
    "PIL.ImageQt",
    "PIL.ImageTk",
]

# Loaded by name inside functions (create_node_app mounts the role; plex_db asks winplatform on Windows only;
# --undo-loudness runs loudness.undo).
HIDDEN = [
    "media_preview_generator.loudness.undo",
    "media_preview_generator.nodes.server.app",
    "media_preview_generator.nodes.server.plex_db",
    "media_preview_generator.nodes.server.winplatform",
    "media_preview_generator.markers.publishers.plex_chapters",
    "waitress",
]

a = Analysis(
    [os.path.join(HERE, "plex_db_node.py")],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

windowed = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PlexDbNode",
    console=False,
    upx=False,
)
debug = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PlexDbNode-debug",
    console=True,
    upx=False,
)
coll = COLLECT(windowed, debug, a.binaries, a.datas, name="PlexDbNode", upx=False)
