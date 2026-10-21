"""Bounded Plex notifications that ask Plex to analyze a file so it advertises its previews."""

from __future__ import annotations

import atexit
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from ..servers.plex_scheduled_jobs import read_settings, should_hold, window_text
from . import plex_held_rereads

if TYPE_CHECKING:
    from ..servers.plex import PlexServer


@dataclass(frozen=True)
class _Refresh:
    server: PlexServer
    canonical_path: str
    item_id: str | None
    library_id: str | None = None
    quiet: bool = False


# Items Plex already analyzed for a file since the app started. One Plex still won't flag afterwards would otherwise
# be re-analyzed on every scan.
_ANALYZED_MEMORY = 4096


class PlexRefreshQueue:
    """Coalesce pending file notifications without blocking generation on Plex.

    One daemon worker drains a bounded queue and exits after an idle period.
    An in-flight key is removed before its request starts so another write
    during that request schedules a second notification.
    """

    def __init__(self, *, capacity: int = 128, idle_seconds: float = 30.0) -> None:
        self._capacity = capacity
        self._idle_seconds = idle_seconds
        self._pending: OrderedDict[tuple[str, str], _Refresh] = OrderedDict()
        self._inflight: set[tuple[str, str]] = set()
        self._completed: OrderedDict[tuple[str, str], bool] = OrderedDict()
        self._analyzed: OrderedDict[tuple[str, str, str | None], None] = OrderedDict()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._closed = False

    def enqueue(
        self,
        server: PlexServer,
        canonical_path: str,
        item_id: str | None,
        *,
        library_id: str | None = None,
        quiet: bool = False,
    ) -> bool:
        """Queue a notification, returning False when capacity is exhausted.

        Args:
            server: Plex server to notify.
            canonical_path: File whose previews Plex should pick up.
            item_id: Plex item id when known.
            quiet: A catch-up request for a file a scan found unflagged, where nothing new was written. These
                fill at most half the queue, so newly written previews still get a place, and a full queue is
                logged at DEBUG instead of WARNING.
        """
        key = (server.id, canonical_path)
        with self._condition:
            if self._closed:
                return False
            # An earlier Analyze's result no longer stands for this file, even when the queue turns this one away:
            # chapters registered now would be replaced by the Analyze a later job sends.
            self._completed.pop(key, None)
            limit = self._capacity // 2 if quiet else self._capacity
            queued = sum(1 for request in self._pending.values() if request.quiet) if quiet else len(self._pending)
            # A file whose Analyze is running always gets its follow-up: one turned away here would be held, and the
            # running request would then release that newer hold and report a completion the new preview never got.
            if key not in self._pending and key not in self._inflight and queued >= limit:
                log = logger.debug if quiet else logger.warning
                log(
                    "Plex notification queue is full; previews for {} are saved, but {} must scan/analyze "
                    "the file later to advertise them to clients. {}",
                    canonical_path,
                    server.name,
                    "A later job re-checks it." if quiet else "The app sends it again within about 10 minutes.",
                )
                return False
            previous = self._pending.get(key)
            self._pending[key] = _Refresh(
                server,
                canonical_path,
                item_id or (previous.item_id if previous else None),
                library_id,
                quiet=quiet and (previous is None or previous.quiet),
            )
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="plex-preview-notifications", daemon=True)
                self._worker.start()
            self._condition.notify_all()
            return True

    def asked(self, server_id: str, canonical_path: str, item_id: str) -> bool:
        """Whether Analyze for this file is queued or running, or Plex already ran it for this item since app start."""
        key = (server_id, canonical_path)
        with self._condition:
            if key in self._pending or key in self._inflight:
                return True
            # An Analyze sent without an item id went to the item Plex found by path.
            return (*key, item_id) in self._analyzed or (*key, None) in self._analyzed

    def is_queued(self, server_id: str, canonical_path: str) -> bool:
        """Whether a notification for this file is waiting or being sent."""
        key = (server_id, canonical_path)
        with self._condition:
            return key in self._pending or key in self._inflight

    def free_slots(self) -> int:
        """How many more files can be queued before the queue is full."""
        with self._condition:
            return self._capacity - len(self._pending)

    def wait(
        self,
        server_id: str,
        canonical_path: str,
        *,
        required: bool = False,
        timeout: float = 30.0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> bool:
        """Wait for this source's Analyze requests before registering chapters.

        Missing completion evidence fails closed when this publication requested
        Analyze. BIF-only callers never wait.
        """
        from .generator import CancellationError

        key = (server_id, canonical_path)
        deadline = time.monotonic() + timeout
        while True:
            if cancel_check and cancel_check():
                raise CancellationError("Chapter publication cancelled while waiting for Plex Analyze")
            with self._condition:
                if self._closed or threading.current_thread() is self._worker:
                    return False
                if key not in self._pending and key not in self._inflight:
                    # Not required: nothing this job asked for is outstanding, and an earlier request that failed
                    # with nothing queued after it can't wipe chapters registered now.
                    return self._completed.get(key, False) if required else True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.2))

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._pending and not self._closed:
                    self._condition.wait(timeout=self._idle_seconds)
                if self._closed or not self._pending:
                    self._worker = None
                    return
                key, request = self._pending.popitem(last=False)
                self._inflight.add(key)
            notified = False
            held = False
            try:
                held = should_hold(request.server, request.library_id)
                if held:
                    self._hold(request)
                    continue
                notified = request.server.refresh_preview_metadata(request.canonical_path, request.item_id)
            except Exception as exc:
                logger.warning(
                    "Previews saved for {}, but {} could not be notified: {}. Plex can activate them on a later analyze.",
                    request.canonical_path,
                    request.server.name,
                    exc,
                )
            finally:
                if not held:
                    plex_held_rereads.release(request.server.id, request.canonical_path)
                with self._condition:
                    self._inflight.discard(key)
                    if notified is True:
                        done = (*key, str(request.item_id).rsplit("/", 1)[-1] if request.item_id else None)
                        self._analyzed[done] = None
                        self._analyzed.move_to_end(done)
                        while len(self._analyzed) > _ANALYZED_MEMORY:
                            self._analyzed.popitem(last=False)
                    self._completed[key] = notified is True
                    self._completed.move_to_end(key)
                    while len(self._completed) > max(256, self._capacity):
                        self._completed.popitem(last=False)
                    self._condition.notify_all()

    @staticmethod
    def _hold(request: _Refresh) -> None:
        """Keep the request for Plex's maintenance window, in a list that survives a restart."""
        settings = read_settings(request.server)
        logger.info(
            "Holding Plex's re-read of {} until its maintenance window ({})",
            request.canonical_path,
            window_text(settings) if settings else "unknown hours",
        )
        plex_held_rereads.hold(
            request.server.id,
            request.canonical_path,
            item_id=request.item_id,
            library_id=request.library_id,
        )

    def close(self) -> None:
        """Discard memory work; a later job re-checks Plex's preview flag after restart."""
        with self._condition:
            self._closed = True
            self._pending.clear()
            self._condition.notify_all()


_queue = PlexRefreshQueue()
atexit.register(_queue.close)


def enqueue_plex_refresh(
    server: PlexServer,
    canonical_path: str,
    item_id: str | None,
    *,
    library_id: str | None = None,
    quiet: bool = False,
) -> bool:
    """Ask Plex to analyze a file asynchronously."""
    return _queue.enqueue(server, canonical_path, item_id, library_id=library_id, quiet=quiet)


def plex_refresh_asked(server_id: str, canonical_path: str, item_id: str) -> bool:
    """Whether Plex is about to analyze this file, or already analyzed it for this item since the app started."""
    return _queue.asked(server_id, canonical_path, item_id)


def is_plex_refresh_queued(server_id: str, canonical_path: str) -> bool:
    """Whether a notification for this file is waiting or being sent."""
    return _queue.is_queued(server_id, canonical_path)


def free_plex_refresh_slots() -> int:
    """How many more files the notification queue has room for."""
    return _queue.free_slots()


def wait_for_plex_refresh(
    server_id: str,
    canonical_path: str,
    *,
    required: bool = False,
    timeout: float = 30.0,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Bound chapter publication behind the source's pending Analyze requests."""
    return _queue.wait(server_id, canonical_path, required=required, timeout=timeout, cancel_check=cancel_check)
