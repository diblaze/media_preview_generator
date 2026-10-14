"""The app's HTTP client for one node (spec §3).

Every call raises one of the ``Node*Error`` classes below on anything but a clean answer, so callers branch on the
class, never on status codes or message text. The key is sent only in the Authorization header and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from . import protocol

# Connect fast (a LAN node either answers or it doesn't); read long enough for a busy node to answer a poll.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0
# /v1/info runs GPU detection on a cold node (measured 0.7-1.3 s; a slow Vulkan probe can take longer).
INFO_TIMEOUT_S = 60.0
# A preview's frames are a few MB; the artifact download gets its own, longer read timeout.
ARTIFACT_TIMEOUT_S = 300.0


class NodeError(Exception):
    """Base class for every node client failure."""


class NodeUnreachableError(NodeError):
    """No answer: connection refused, timed out, DNS, TLS, or a 5xx from something in between."""


class NodeAuthError(NodeError):
    """The node refused the key (401)."""


class NodeProtocolError(NodeError):
    """The node speaks another protocol, or answered with something that isn't this protocol."""


class NodeUnknownTaskError(NodeError):
    """The node doesn't know the task (it restarted, or expired the task)."""


class NodeRequestError(NodeError):
    """The node refused the request as malformed (400). A bug on one side, never a user problem."""


@dataclass(frozen=True)
class NodeAddress:
    """Where a node is and the key it expects."""

    url: str
    key: str

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


class NodeClient:
    """Calls one node's routes. Thread-safe: one ``requests.Session`` per client, used under its own lock-free API.

    Args:
        address: The node's URL and key.
        session: For tests.
    """

    def __init__(self, address: NodeAddress, *, session: requests.Session | None = None) -> None:
        self._address = address
        self._session = session or requests.Session()

    @property
    def url(self) -> str:
        return self._address.base

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._address.key}", protocol.PROTOCOL_HEADER: str(protocol.NODE_PROTOCOL)}

    def _call(
        self, method: str, path: str, *, json: dict | None = None, read_timeout: float = READ_TIMEOUT_S,
        auth: bool = True, raw: bool = False,
    ) -> Any:  # fmt: skip
        try:
            response = self._session.request(
                method,
                self._address.base + path,
                json=json,
                headers=self._headers() if auth else None,
                timeout=(CONNECT_TIMEOUT_S, read_timeout),
            )
        except requests.RequestException as exc:
            raise NodeUnreachableError(f"{self._address.base} didn't answer: {type(exc).__name__}") from exc
        if response.status_code == 401:
            raise NodeAuthError(f"{self._address.base} refused the key")
        if response.status_code >= 500:
            raise NodeUnreachableError(f"{self._address.base} answered HTTP {response.status_code}")
        if raw and response.status_code == 200:
            return response.content
        try:
            body = response.json()
        except ValueError as exc:
            raise NodeProtocolError(
                f"{self._address.base} didn't answer as a node (HTTP {response.status_code})"
            ) from exc
        if not isinstance(body, dict):
            raise NodeProtocolError(f"{self._address.base} didn't answer as a node")
        raw_error = body.get("error")
        error: dict = raw_error if isinstance(raw_error, dict) else {}
        kind = error.get("kind")
        message = str(error.get("message") or kind or f"HTTP {response.status_code}")
        if response.status_code == 404 and kind == protocol.KIND_UNKNOWN_TASK:
            raise NodeUnknownTaskError(message)
        if response.status_code == 404:
            # A Plex helper from before nodes existed answers its own routes but 404s the node ones.
            raise NodeProtocolError(f"{self._address.base} doesn't know {path} (an older Plex helper?)")
        if response.status_code == 409 and kind == protocol.KIND_PROTOCOL:
            raise NodeProtocolError(message)
        if response.status_code == 400:
            raise NodeRequestError(message)
        if response.status_code >= 400:
            raise NodeProtocolError(f"{self._address.base} answered HTTP {response.status_code}: {message}")
        return body

    def health(self) -> dict:
        """``GET /v1/health`` (no key needed): the node block, or the legacy agent block of an old helper."""
        return self._call("GET", protocol.ROUTE_HEALTH, auth=False)

    def info(self) -> dict:
        """``GET /v1/info``: identity, roles, GPUs, CPU/RAM/temp, Plex database facts."""
        return self._call("GET", protocol.ROUTE_INFO, read_timeout=INFO_TIMEOUT_S)

    def rescan_gpus(self) -> dict:
        """``POST /v1/gpus/rescan``: run GPU detection again and answer with the fresh list."""
        return self._call("POST", protocol.ROUTE_GPUS_RESCAN, json={}, read_timeout=INFO_TIMEOUT_S)

    def access_check(self, mappings: list[protocol.PathMapping], files: list[dict]) -> dict:
        """``POST /v1/access-check`` for up to a few dozen ``{"path", "size", "mtime_ns"}`` files."""
        body = {"mapping": [m.to_json() for m in mappings], "files": files}
        return self._call("POST", protocol.ROUTE_ACCESS_CHECK, json=body, read_timeout=30.0)

    def start_task(self, spec: dict) -> str:
        """``POST /v1/tasks``; returns the task id."""
        body = self._call("POST", protocol.ROUTE_TASKS, json=spec)
        task_id = body.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise NodeProtocolError(f"{self._address.base} started a task without an id")
        return task_id

    def poll_task(self, task_id: str, *, log_from: int = 0) -> dict:
        """``GET /v1/tasks/<id>?log_from=N``: the task state with log lines from ``log_from`` on."""
        return self._call("GET", f"{protocol.ROUTE_TASKS}/{task_id}?log_from={int(log_from)}")

    def cancel_task(self, task_id: str) -> None:
        """``POST /v1/tasks/<id>/cancel``."""
        self._call("POST", f"{protocol.ROUTE_TASKS}/{task_id}/cancel", json={})

    def fetch_artifact(self, task_id: str) -> bytes:
        """``GET /v1/tasks/<id>/artifact``: the task's artifact bytes (a preview's frames as a tar)."""
        return self._call(
            "GET", f"{protocol.ROUTE_TASKS}/{task_id}/artifact", read_timeout=ARTIFACT_TIMEOUT_S, raw=True
        )

    def delete_task(self, task_id: str) -> None:
        """``DELETE /v1/tasks/<id>``: free the task's temp dir on the node."""
        self._call("DELETE", f"{protocol.ROUTE_TASKS}/{task_id}")

    def log(self, lines: int = 200) -> dict:
        """``GET /v1/log?lines=N``: the node's own recent log."""
        return self._call("GET", f"{protocol.ROUTE_LOG}?lines={int(lines)}")
