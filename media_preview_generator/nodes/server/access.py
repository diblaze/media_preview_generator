"""From the app's path to this node's file, refusing anything the mappings don't cover.

The app sends its own path, the mapping rows, and the size and mtime_ns it saw. The node maps the path, resolves it
(``..`` and symlinks included) and refuses it unless the real path is inside one of the mapped roots, then checks the
size and mtime_ns so a different file at the same path is never processed in its place (measured identical across
ZFS, NFS 4.2, bind mounts and mergerfs; dev and inode are not, so they aren't compared).
"""

from __future__ import annotations

import os
import stat

from ..protocol import (
    NOT_VISIBLE_DIFFERENT_FILE,
    NOT_VISIBLE_MISSING,
    NOT_VISIBLE_OUTSIDE_ROOTS,
    NOT_VISIBLE_UNMAPPED,
    NOT_VISIBLE_UNREADABLE,
    PathMapping,
    map_app_path,
)

NEARBY_MAX_NAMES = 50


class NotVisible(Exception):
    """This node can't use the file; ``reason`` is one of the protocol's ``NOT_VISIBLE_*``."""

    def __init__(self, reason: str, message: str, *, node_path: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.node_path = node_path


def _within(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows, or a relative path
        return False


def mapped_roots(mappings: list[PathMapping]) -> list[str]:
    """The real paths of every row's ``node_prefix``."""
    return [os.path.realpath(row.node_prefix) for row in mappings]


def inside_roots(path: str, mappings: list[PathMapping]) -> bool:
    """Whether ``path``'s real path is inside one of the mapped roots."""
    real = os.path.realpath(path)
    return any(_within(real, root) for root in mapped_roots(mappings))


def resolve(app_path: str, size: int | None, mtime_ns: int | None, mappings: list[PathMapping]) -> str:
    """This node's real path for the app's file, after every check.

    Args:
        app_path: The app's path.
        size: The size the app saw (None: not compared).
        mtime_ns: The mtime the app saw, in ns (None: not compared).
        mappings: The node's mapping rows.

    Returns:
        The real path on this node.

    Raises:
        NotVisible: Unmapped, outside the mapped roots, missing, a different file, or unreadable.
    """
    mapped = map_app_path(app_path, mappings, node_sep=os.sep)
    if mapped is None:
        raise NotVisible(NOT_VISIBLE_UNMAPPED, f"No path mapping covers {app_path}")
    real = os.path.realpath(mapped)
    if not any(_within(real, root) for root in mapped_roots(mappings)):
        raise NotVisible(
            NOT_VISIBLE_OUTSIDE_ROOTS, f"{mapped} resolves to {real}, outside every mapped folder", node_path=mapped
        )
    try:
        st = os.stat(real)
    except (FileNotFoundError, NotADirectoryError):
        raise NotVisible(NOT_VISIBLE_MISSING, f"{mapped} doesn't exist on this node", node_path=mapped) from None
    except PermissionError:
        raise NotVisible(NOT_VISIBLE_UNREADABLE, f"This node may not open {mapped}", node_path=mapped) from None
    except OSError as exc:
        raise NotVisible(
            NOT_VISIBLE_UNREADABLE, f"This node can't read {mapped}: {exc.strerror or exc}", node_path=mapped
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise NotVisible(NOT_VISIBLE_MISSING, f"{mapped} isn't a file on this node", node_path=mapped)
    if size is not None and st.st_size != size:
        raise NotVisible(
            NOT_VISIBLE_DIFFERENT_FILE,
            f"{mapped} is a different file here: {st.st_size} bytes, the app's has {size}",
            node_path=mapped,
        )
    if mtime_ns is not None and st.st_mtime_ns != mtime_ns:
        raise NotVisible(
            NOT_VISIBLE_DIFFERENT_FILE,
            f"{mapped} is a different file here: modified at another time than the app's",
            node_path=mapped,
        )
    if not os.access(real, os.R_OK):
        raise NotVisible(NOT_VISIBLE_UNREADABLE, f"This node may not read {mapped}", node_path=mapped)
    return real


def nearby(node_path: str, mappings: list[PathMapping]) -> tuple[str, list[str]] | None:
    """The deepest existing folder above a missing file and up to 50 of its names, so a user can spot the right path.

    Args:
        node_path: The mapped (missing) path.
        mappings: The node's mapping rows; nothing outside their roots is ever listed.

    Returns:
        ``(folder, names)``, or None when no folder inside the mapped roots exists above it.
    """
    roots = mapped_roots(mappings)
    folder = os.path.dirname(node_path)
    while folder:
        if os.path.isdir(folder):
            if not any(_within(os.path.realpath(folder), root) for root in roots):
                return None
            try:
                names = sorted(os.listdir(folder))[:NEARBY_MAX_NAMES]
            except OSError:
                return None
            return folder, names
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return None
