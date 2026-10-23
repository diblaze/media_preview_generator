"""Files a feature can never finish, so Library Health counts them as "can't" instead of "to do".

Jobs record a failure where it's known for certain and keep trying the file as before; a record only changes how the
check counts, and only while the file still has the size and mtime it had when it failed.
"""

from __future__ import annotations

import os

from loguru import logger

from .markers_db import path_key
from .models import Feature
from .store import HealthStore, default_store

# The intro/credits step's failure for a file whose length can't be read (markers/pipeline.py words it).
NO_DURATION_MESSAGE = "Couldn't read the file's duration"


def record_failure(features: tuple[Feature, ...], path: str, reason: str) -> None:
    """Remember that these features can't be done for ``path`` as it is now. Never raises."""
    try:
        info = os.stat(path)
        store = default_store()
        for feature in features:
            store.record_failure(feature, path, info.st_size, info.st_mtime_ns, reason)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail a job
        logger.debug("Could not record the failure for {}: {}", path, exc)


def clear_failure(features: tuple[Feature, ...], path: str) -> None:
    """Forget the recorded failures of these features for ``path``. Never raises."""
    try:
        store = default_store()
        for feature in features:
            store.clear_failure(feature, path)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never fail a job
        logger.debug("Could not clear the recorded failure for {}: {}", path, exc)


def unreadable_paths(store: HealthStore) -> dict[Feature, frozenset[str]]:
    """Per feature, the recorded paths whose file still has the size and mtime it had when it failed.

    Only the few recorded paths are stat'ed, never the library. Paths come back as ``path_key``s.
    """
    still_failing: dict[Feature, set[str]] = {}
    for (feature, path), (size, mtime_ns) in store.failures().items():
        try:
            info = os.stat(path)
        except OSError:
            continue
        if (info.st_size, info.st_mtime_ns) == (size, mtime_ns):
            still_failing.setdefault(feature, set()).add(path_key(path))
    return {feature: frozenset(paths) for feature, paths in still_failing.items()}
