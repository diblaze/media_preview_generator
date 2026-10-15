"""The last Test access result of each node, kept across restarts so Setup Health doesn't ask for a new test.

One small JSON file beside ``settings.json`` (``node_access_results.json``), not inside it: a test runs often and its
result is a cache of what the node saw, not a setting. Each entry keeps the address and mappings it was tested with;
an entry for another address (another machine) or other mappings than the node's saved ones is never handed out, so
changing either invalidates its result.

``{"<node id>": {"url": address, "mappings": [{"app_prefix", "node_prefix"}], "checked_at": epoch seconds,
"result": {...}}}``
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Collection
from typing import Any

from loguru import logger

FILE_NAME = "node_access_results.json"


def _default_config_dir() -> str:
    from ..web.settings_manager import get_settings_manager

    return str(get_settings_manager().config_dir)


def _mapping_key(mappings: object) -> list[dict[str, str]]:
    rows = mappings if isinstance(mappings, list) else []
    return [
        {"app_prefix": str(row.get("app_prefix") or ""), "node_prefix": str(row.get("node_prefix") or "")}
        for row in rows
        if isinstance(row, dict)
    ]


class AccessResultStore:
    """Reads and writes the file; a broken or missing file reads as empty.

    Args:
        config_dir: Where the file lives (default: the settings manager's config dir, read at each use).
    """

    def __init__(self, config_dir: Callable[[], str] = _default_config_dir) -> None:
        self._config_dir = config_dir
        self._lock = threading.Lock()

    def _path(self) -> str:
        return os.path.join(self._config_dir(), FILE_NAME)

    def _read(self) -> dict[str, Any]:
        try:
            with open(self._path(), encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Couldn't read the saved Test access results ({}); nodes will ask for a new test", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        path = self._path()
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".node_access_results-", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def load(self, node_id: str, url: str, mappings: object) -> tuple[dict, float | None] | None:
        """The node's saved result and when it ran, when it was tested at ``url`` with exactly ``mappings``; else
        None.

        Args:
            node_id: The node.
            url: Its saved address.
            mappings: Its saved ``path_mappings`` rows.
        """
        with self._lock:
            entry = self._read().get(node_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("result"), dict):
            return None
        if entry.get("url") != url or entry.get("mappings") != _mapping_key(mappings):
            return None
        checked_at = entry.get("checked_at")
        return entry["result"], float(checked_at) if isinstance(checked_at, int | float) else None

    def save(
        self,
        node_id: str,
        url: str,
        mappings: object,
        result: dict,
        checked_at: float,
        *,
        keep: Collection[str] = (),
    ) -> None:
        """Remember ``result`` for the node; entries for nodes not in ``keep`` (removed nodes) are dropped.

        A failed write is logged, never raised: the result still holds in memory until a restart.

        Args:
            node_id: The node.
            url: The address it was tested at.
            mappings: The ``path_mappings`` rows it was tested with.
            result: ``{"libraries": [...]}``.
            checked_at: Epoch seconds.
            keep: The node ids still configured (the tested node is always kept).
        """
        with self._lock:
            data = {k: v for k, v in self._read().items() if k in keep or k == node_id}
            data[node_id] = {
                "url": url,
                "mappings": _mapping_key(mappings),
                "checked_at": checked_at,
                "result": result,
            }
            try:
                self._write(data)
            except OSError as exc:
                logger.warning("Couldn't save the Test access result of node {}: {}", node_id, exc)

    def forget(self, node_id: str, *, keep_url: str | None = None, keep_mappings: object = None) -> None:
        """Drop the node's saved result (its address or mappings changed).

        Args:
            node_id: The node.
            keep_url: The node's address, when only its mappings changed.
            keep_mappings: The node's new ``path_mappings``: a result already saved at ``keep_url`` for exactly these
                (a test that finished before this ran) is kept.
        """
        with self._lock:
            data = self._read()
            entry = data.get(node_id)
            if entry is None:
                return
            if (
                keep_mappings is not None
                and isinstance(entry, dict)
                and entry.get("url") == keep_url
                and entry.get("mappings") == _mapping_key(keep_mappings)
            ):
                return
            del data[node_id]
            try:
                self._write(data)
            except OSError as exc:
                logger.warning("Couldn't drop the saved Test access result of node {}: {}", node_id, exc)
