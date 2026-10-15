"""From the app's path to this node's file, refusing anything the mappings don't cover.

The app sends its own path, the mapping rows, and the size and mtime_ns it saw. The node maps the path, resolves it
(``..`` and symlinks included) and refuses it unless the real path is inside one of the mapped roots, then checks the
size and mtime so a different file at the same path is never processed in its place. Sizes must be equal; mtimes may
differ by less than :data:`MTIME_TOLERANCE_NS`, because the node may read the files through a filesystem with coarser
timestamps than the app's (dev and inode differ across mounts, so they aren't compared).
"""

from __future__ import annotations

import os
import stat
import unicodedata

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
# ZFS, ext4, NFS 4.x keep nanoseconds; SMB2/3 keeps 100 ns, sshfs and SMB1 whole seconds, FAT 2 seconds. The same file
# seen through any of them stays within FAT's granularity; a different file at the same path rarely does, and its
# size must match exactly too.
MTIME_TOLERANCE_NS = 2_000_000_000


class NotVisible(Exception):
    """This node can't use the file; ``reason`` is one of the protocol's ``NOT_VISIBLE_*``."""

    def __init__(self, reason: str, message: str, *, node_path: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.node_path = node_path


def _closest_first(names: list[str], node_path: str) -> list[str]:
    """``names`` sorted, those named like a folder in ``node_path`` first, cut to :data:`NEARBY_MAX_NAMES`.

    The app suggests a node folder named like one it asked for, so a long listing (Unraid's ``/mnt/user``) must not
    cut that folder off just because it sorts late.
    """

    def key(name: str) -> str:
        return unicodedata.normalize("NFD", name).casefold()

    wanted = {key(part) for part in node_path.replace("\\", "/").split("/") if part}
    return sorted(names, key=lambda name: (key(name) not in wanted, name))[:NEARBY_MAX_NAMES]


def _within(path: str, root: str) -> bool:
    path, root = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # different drives on Windows, or a relative path
        return False


def same_mtime(app_mtime_ns: int, node_mtime_ns: int) -> bool:
    """Whether two mtimes can be the same file's, seen through filesystems that keep different precision."""
    return abs(app_mtime_ns - node_mtime_ns) < MTIME_TOLERANCE_NS


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
        mtime_ns: The mtime the app saw, in ns (None: not compared; otherwise it must be within 2 s of the node's).
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
    if mtime_ns is not None and not same_mtime(mtime_ns, st.st_mtime_ns):
        raise NotVisible(
            NOT_VISIBLE_DIFFERENT_FILE,
            f"{mapped} is a different file here: its modified time is 2 or more seconds off the app's",
            node_path=mapped,
        )
    if not os.access(real, os.R_OK):
        raise NotVisible(NOT_VISIBLE_UNREADABLE, f"This node may not read {mapped}", node_path=mapped)
    return real


def nearby(node_path: str, mappings: list[PathMapping]) -> tuple[str, list[str]] | None:
    """A folder listing near a missing file, so a user can spot the right path.

    Inside the mapped roots, the deepest existing folder above the file and up to 50 of its names. When the row's own
    node folder doesn't exist (the usual mistake: a misnamed folder), the folder names beside it instead.

    Args:
        node_path: The mapped (missing) path.
        mappings: The node's mapping rows; nothing outside their roots is listed, except the folder names beside a
            row's missing node folder.

    Returns:
        ``(folder, names)``, or None when there is nothing to list.
    """
    roots = mapped_roots(mappings)
    folder = os.path.dirname(node_path)
    while folder:
        if os.path.isdir(folder):
            if not any(_within(os.path.realpath(folder), root) for root in roots):
                break
            try:
                names = _closest_first(os.listdir(folder), node_path)
            except OSError:
                return None
            return folder, names
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return _beside_missing_root(node_path, mappings)


def _beside_missing_root(node_path: str, mappings: list[PathMapping]) -> tuple[str, list[str]] | None:
    """The folder names in the parent of the missing node folder of the row ``node_path`` was mapped through.

    The request's own rows choose the roots, so naming the folders one level above a root it names shows no more
    than a row for that parent would; files and anything deeper stay unlisted.
    """
    path = os.path.normpath(node_path)
    covering = [row for row in mappings if _within(path, os.path.normpath(row.node_prefix))]
    if not covering:
        return None
    prefix = os.path.normpath(max(covering, key=lambda row: len(row.node_prefix)).node_prefix)
    parent = os.path.dirname(prefix)
    if parent == prefix or os.path.exists(prefix) or not os.path.isdir(parent):
        return None
    try:
        with os.scandir(parent) as entries:
            names = [entry.name for entry in entries if _is_dir(entry)]
    except OSError:
        return None
    return parent, _closest_first(names, node_path)


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False
