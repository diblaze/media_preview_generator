"""What an Intro & Credits job shares between the episodes of one season.

Checking a season's 25 episodes one after another would list its folders 25 times and read every sibling's record 25
times. The job keeps one entry per season folder instead, for a short time (``ttl_s``), so the first episode checked
reads the season and the next ones reuse it.

Three kinds of facts are kept:

* What the disks say (the season's video files, the previous season's first episodes, each sibling's size and mtime).
  Before an entry is reused, each folder of the season is stat'd and its mtime compared with the one recorded when the
  listing was read (:meth:`SeasonEntry.watch`); a file added, removed or renamed (a Sonarr import) changes it, so the
  entry is rebuilt, and so it is when a folder can't be stat'd. What the folder's mtime can't show, and so can be up to
  ``ttl_s`` old, is a sibling overwritten in place (same name, new size or mtime) and a change in the previous season's
  folders. That is acceptable: the file being checked always stats itself and decides on its own identity, and the
  sibling's own check does the same when its turn comes. An episode checked just before its sibling was overwritten
  could see the old sibling before this cache existed too (the order of two checks was always a race).
* What ``markers.db`` says about the season's files (their signature items, signatures and intro-chapter lengths).
  Valid only while ``MarkerStore.data_generation`` is the value it was read at: any write to the tables they come from,
  by any thread, ends them before the next read.
* What the folders a season's show is looked for in say (a TV library's folders, holding thousands of shows, read for
  every season of the job): :meth:`SeasonCache.listing`. A listing is reused while the folder's device, inode and
  mtime are the ones stat'd before it was read (a show added, removed or renamed moves the mtime; a remount moves the
  device or inode), and a folder whose mtime is less than ``_SETTLED_NS`` from now is never kept, so a change made in
  the same mtime tick as the listing (coarse timestamps, a network disk's clock) can't hide. What the mtime can't show
  is a folder that doesn't move its own mtime when what it lists changes (a pooled disk that reports one member's
  mtime) and a folder that stats but can't be listed any more: for up to ``ttl_s`` the last listing stands, as if the
  check had run just before the change (the order of a check and a change was always a race). The season's own
  folders are still listed every time their entry is built, so a disk holding the show that can't be read is seen.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Collection
from typing import Any

# Entries kept before the expired ones are dropped (a job over a large library meets thousands of seasons).
_PRUNE_ABOVE = 256
# A folder changed less than this long ago (by its mtime against this clock, either way) isn't kept by
# ``SeasonCache.listing``: a change in the same mtime tick as the listing, or stamped by a disk whose clock is a little
# off, could otherwise go unseen. Library folders change rarely, so nearly every listing is kept.
_SETTLED_NS = 120 * 1_000_000_000


def _folder_stamp(folder: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(folder)
    except OSError:
        return None
    return st.st_dev, st.st_ino, st.st_mtime_ns


def _folder_mtime_ns(folder: str) -> int | None:
    try:
        return os.stat(folder).st_mtime_ns
    except OSError:
        return None


class SeasonResults:
    """What ``markers.db`` said about a season's files while ``MarkerStore.data_generation`` was ``generation``."""

    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.items: dict[str, list] = {}
        self.signatures: dict[str, str] = {}
        self.intro_chapter_ms: dict[str, int | None] = {}


class SeasonEntry:
    """One season folder's shared facts."""

    def __init__(self, built_at: float) -> None:
        self.built_at = built_at
        self._lock = threading.Lock()
        self._building: dict[str, threading.Lock] = {}
        self._once: dict[str, Any] = {}
        self._results = SeasonResults(-1)
        self._watched: dict[str, int | None] = {}

    def watch(self, folders: Collection[str]) -> None:
        """Record the folders' mtimes (None for one that can't be stat'd), to compare with in :meth:`folders_changed`.
        Called before the folders are listed, so a change while they are listed shows up at the next check."""
        self._watched = {folder: _folder_mtime_ns(folder) for folder in folders}

    def folders_changed(self) -> bool:
        """Whether a watched folder's mtime differs from the recorded one, or it can't be stat'd now."""
        for folder, recorded in self._watched.items():
            current = _folder_mtime_ns(folder)
            if current is None or current != recorded:
                return True
        return False

    def once(self, key: str, build: Callable[[], Any]) -> Any:
        """A fact built the first time it is asked for. Other threads asking for it meanwhile wait for the one build,
        and see only its finished value (stored after ``build`` returns); a build that raises stores nothing, and the
        next asker builds. Each fact has its own build lock: a season's listing on a slow disk keeps no one waiting for
        another fact (the previous season's files) or for :meth:`results`."""
        with self._lock:
            if key in self._once:
                return self._once[key]
            building = self._building.setdefault(key, threading.Lock())
        with building:
            with self._lock:
                if key in self._once:
                    return self._once[key]
            value = build()
            with self._lock:
                self._once[key] = value
            return value

    def results(self, generation: int) -> SeasonResults:
        """The store-derived facts valid at ``generation``: the ones read at it, else a new empty set."""
        with self._lock:
            if self._results.generation != generation:
                self._results = SeasonResults(generation)
            return self._results


class SeasonCache:
    """A job's season entries, one per season folder. With ``ttl_s`` 0 nothing is kept: every ask gets a new entry."""

    def __init__(self, ttl_s: float = 0.0, clock: Callable[[], float] = time.monotonic) -> None:
        """Create the cache.

        Args:
            ttl_s: How long an entry is reused, seconds on ``clock``.
            clock: A monotonic clock (tests use a fake one).
        """
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, SeasonEntry] = {}
        self._listings: dict[str, tuple[tuple[int, int, int], float, Any]] = {}

    def entry(self, folder: str) -> SeasonEntry:
        """The folder's entry, a new one when it has none, it is older than ``ttl_s`` or a folder it watches changed."""
        now = self._clock()
        with self._lock:
            held = self._entries.get(folder)
        if held is not None and now - held.built_at < self._ttl_s and not held.folders_changed():
            return held
        with self._lock:
            current = self._entries.get(folder)
            if current is not None and current is not held and now - current.built_at < self._ttl_s:
                return current  # another thread rebuilt it meanwhile
            fresh = SeasonEntry(now)
            if self._ttl_s > 0:
                if len(self._entries) > _PRUNE_ABOVE:
                    self._entries = {f: e for f, e in self._entries.items() if now - e.built_at < self._ttl_s}
                self._entries[folder] = fresh
            return fresh

    def forget(self, folder: str, entry: SeasonEntry) -> None:
        """Drop ``entry`` (not a newer one of the folder) so the next ask reads the season again."""
        with self._lock:
            if self._entries.get(folder) is entry:
                del self._entries[folder]

    def listing(self, folder: str, read: Callable[[], Any]) -> Any:
        """What ``read`` makes of a folder's listing, reused for ``ttl_s`` while the folder's device, inode and mtime stay
        the ones stat'd before it was read. Nothing is kept when the folder can't be stat'd or changed less than
        ``_SETTLED_NS`` ago, and an error ``read`` raises is passed on (nothing kept either).

        Args:
            folder: The folder.
            read: Lists it (raising ``OSError`` when it can't be).

        Returns:
            ``read()``'s value, this time's or a recent one's.
        """
        now = self._clock()
        stamp = _folder_stamp(folder)
        with self._lock:
            held = self._listings.get(folder)
        if held is not None and stamp is not None and held[0] == stamp and now - held[1] < self._ttl_s:
            return held[2]
        value = read()
        if stamp is not None and self._ttl_s > 0 and abs(time.time_ns() - stamp[2]) >= _SETTLED_NS:
            with self._lock:
                if len(self._listings) > _PRUNE_ABOVE:
                    self._listings = {f: kept for f, kept in self._listings.items() if now - kept[1] < self._ttl_s}
                self._listings[folder] = (stamp, now, value)
        return value
