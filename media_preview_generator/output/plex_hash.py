"""Derive Plex's media bundle hash from a stable local source file.

For files of at least 64 KiB, Plex hashes the decimal size followed by the
hexadecimal SHA-1 digests of the first and last 64 KiB. Smaller files use the
size followed by the file's SHA-1 digest directly, without the outer hash.
These boundary rules were verified against Plex 1.43.4.10903's database and
API; the commonly cited forum recipe omits both small-file exceptions.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Sequence
from functools import lru_cache

SourceFingerprint = tuple[int, int, int, int, int]
_BLOCK_SIZE = 65_536


class SourceFileChangedError(OSError):
    """The source changed while its preview or bundle hash was being built."""


def _fingerprint(info: os.stat_result) -> SourceFingerprint:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def same_source(a: Sequence[int] | None, b: Sequence[int] | None) -> bool:
    """Whether two source fingerprints describe the same, unchanged file.

    The change time (the fifth field) never counts: a hard link, chmod or *arr re-import moves it without touching
    the content.

    Args:
        a: A ``get_source_fingerprint`` result, or one saved as a JSON list.
        b: Another, likewise.

    Returns:
        True when both are present, have all five fields, and their device, inode, size and modification time are
        equal.
    """
    if not isinstance(a, tuple | list) or not isinstance(b, tuple | list) or len(a) < 5 or len(b) < 5:
        return False
    return tuple(a[:4]) == tuple(b[:4])


def _opened_is(opened: SourceFingerprint, expected: SourceFingerprint) -> bool:
    """Whether the file just opened (its ``os.fstat``) is the one ``expected`` (an ``os.stat``) describes."""
    return same_source(opened, expected)


def get_source_fingerprint(path: str | os.PathLike[str]) -> SourceFingerprint:
    """Return identity and change markers for a regular media file.

    Args:
        path: Local media filename.

    Returns:
        Device, inode, size, modification time and change time.

    Raises:
        OSError: The source cannot be inspected or is not a regular file.
    """
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise OSError(f"Media source is not a regular file: {path}")
    return _fingerprint(info)


@lru_cache(maxsize=1024)
def _hash_stable_source(path: str, expected: SourceFingerprint) -> str:
    with open(path, "rb") as source:
        opened = _fingerprint(os.fstat(source.fileno()))
        if not _opened_is(opened, expected):
            raise SourceFileChangedError(f"Media source changed before hashing: {path}")
        size = expected[2]
        first = source.read(_BLOCK_SIZE)
        if len(first) != min(size, _BLOCK_SIZE):
            raise SourceFileChangedError(f"Media source changed while hashing: {path}")
        signature = str(size) + hashlib.sha1(first, usedforsecurity=False).hexdigest()
        if size >= _BLOCK_SIZE:
            source.seek(size - _BLOCK_SIZE)
            last = source.read(_BLOCK_SIZE)
            if len(last) != _BLOCK_SIZE:
                raise SourceFileChangedError(f"Media source changed while hashing: {path}")
            signature += hashlib.sha1(last, usedforsecurity=False).hexdigest()
        if not same_source(_fingerprint(os.fstat(source.fileno())), opened):
            raise SourceFileChangedError(f"Media source changed while hashing: {path}")
    if size < _BLOCK_SIZE:
        return signature
    return hashlib.sha1(signature.encode("ascii"), usedforsecurity=False).hexdigest()


def calculate_plex_hash(path: str | os.PathLike[str], *, fingerprint: SourceFingerprint | None = None) -> str:
    """Calculate the current file's Plex bundle hash without a server lookup.

    Results are cached by file identity and change markers, so checks and
    publishers for the same stable source share the bounded media reads.
    Plex metadata is deliberately not used: it can still describe an older
    file that an importer has replaced at the same path.

    Args:
        path: Local media filename.
        fingerprint: The file's ``get_source_fingerprint`` the caller already read. The hash is then computed for
            exactly that file state, and the closing re-stat is left to the caller, who re-checks the file after
            using the hash.

    Returns:
        The Plex media bundle identifier. Small files include a decimal size
        prefix and therefore do not have a fixed 40-character length.

    Raises:
        OSError: The file is unavailable or is not a regular file.
        SourceFileChangedError: The source was replaced or modified during hashing.
    """
    filename = os.path.abspath(path)
    if fingerprint is not None:
        return _hash_stable_source(filename, fingerprint)
    expected = get_source_fingerprint(filename)
    result = _hash_stable_source(filename, expected)
    if not same_source(get_source_fingerprint(filename), expected):
        raise SourceFileChangedError(f"Media source changed while hashing: {filename}")
    return result
