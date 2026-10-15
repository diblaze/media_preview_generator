"""The app's HTTP client for one node (spec §3).

Every call raises one of the ``Node*Error`` classes below on anything but a clean answer, so callers branch on the
class, never on status codes or message text. The key is sent only in the Authorization header and never logged.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from json import loads as parse_json
from typing import Any

import requests
from urllib3.exceptions import NameResolutionError, NewConnectionError, ProtocolError

from . import protocol

# Connect fast (a LAN node either answers or it doesn't); read long enough for a busy node to answer a poll.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0
# /v1/info never waits on GPU detection, so a node that doesn't answer it this fast is stuck; three tries of it then
# call a hung node offline within a minute, while the files it dropped are held back from it.
INFO_TIMEOUT_S = 10.0
# A rescan runs GPU detection while the request waits (measured 0.7-1.3 s; a slow Vulkan probe can take longer).
GPU_RESCAN_TIMEOUT_S = 60.0
# A preview's frames are a few MB; the artifact download gets its own, longer read timeout.
ARTIFACT_TIMEOUT_S = 300.0
# The longest plausible preview (a 4-hour film at the 1 s interval: 14,400 frames of tens of KB each) is well under
# this; a node sending more is refused rather than held in memory.
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
# Every other answer is a small JSON object; the largest (a long film's audio fingerprint) is a few MB.
MAX_ANSWER_BYTES = 64 * 1024 * 1024


class NodeError(Exception):
    """Base class for every node client failure."""


# Why nothing answered (``NodeUnreachableError.reason``), so a caller can say what to check.
UNREACHABLE_REFUSED = "refused"
UNREACHABLE_TIMEOUT = "timeout"
UNREACHABLE_DNS = "dns"
UNREACHABLE_TLS = "tls"


class NodeUnreachableError(NodeError):
    """No answer: connection refused, timed out, DNS, TLS, or a 5xx from something in between.

    Args:
        message: What went wrong, in plain words.
        reason: One of the ``UNREACHABLE_*`` codes, or "" when it isn't known.
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class NodeTimeoutError(NodeUnreachableError):
    """The request reached the node but no answer came: it didn't come in time, or the connection dropped before it
    did. Unlike a refused connection, the node may have acted on it."""


class NodeAuthError(NodeError):
    """The node refused the key (401)."""


class NodeProtocolError(NodeError):
    """The node speaks another protocol, or answered with something that isn't this protocol.

    Args:
        message: What went wrong.
        node: The ``node`` block of a node that refused this app's protocol (it names the node's version); None when
            whatever answered isn't a node of this app (an older Plex helper answers without one).
        web_page: What answered sent a web page (HTML), not a node's JSON.
        app_instance: What answered is this app's web side (``protocol.APP_INSTANCE_HEADER``): its instance id.
    """

    def __init__(
        self, message: str, *, node: dict | None = None, web_page: bool = False, app_instance: str = ""
    ) -> None:
        super().__init__(message)
        self.node = node
        self.web_page = web_page
        self.app_instance = app_instance


class NodeUnknownTaskError(NodeError):
    """The node doesn't know the task (it restarted, or expired the task)."""


class NodeRequestError(NodeError):
    """The node refused the request (400, kind ``request``): it is malformed, or asks for a role the node wasn't
    started with (a GPU rescan or an access check on a node without the worker role)."""


class NodeCantStartError(NodeError):
    """The node couldn't take the task (400, kind ``failed``: its temp folder is full or can't be written). Nothing
    ran, so the file can go to another worker."""


class NodeTaskDeletedError(NodeError):
    """The node refused a start whose task id this app had already deleted (409 ``task_deleted``): nothing ran."""


def _unreachable(base: str, exc: requests.RequestException) -> NodeUnreachableError:
    """A request that got no answer, saying why in plain words (never the exception's class name)."""
    cause = getattr(exc.args[0], "reason", None) if exc.args else None
    if isinstance(exc, requests.exceptions.SSLError):
        return NodeUnreachableError(f"{base} didn't answer over https", reason=UNREACHABLE_TLS)
    if isinstance(cause, NameResolutionError):
        host = base.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
        return NodeUnreachableError(
            f"{base} didn't answer (no machine called {host} was found)", reason=UNREACHABLE_DNS
        )
    if isinstance(exc, requests.Timeout):
        cls = NodeTimeoutError if isinstance(exc, requests.ReadTimeout) else NodeUnreachableError
        return cls(f"{base} didn't answer in time", reason=UNREACHABLE_TIMEOUT)
    if isinstance(cause, NewConnectionError) and "refused" in str(cause).lower():
        return NodeUnreachableError(f"{base} didn't answer (connection refused)", reason=UNREACHABLE_REFUSED)
    return NodeUnreachableError(f"{base} didn't answer (no connection)")


def _answer_lost(exc: requests.ConnectionError) -> bool:
    """Whether the connection dropped after it was made (closed or reset mid-request), so the node may have the
    request; a connection never made (refused, DNS) reaches requests as ``MaxRetryError`` instead."""
    reason = exc.args[0] if exc.args else None
    return any(isinstance(cause, ProtocolError | ConnectionResetError) for cause in (reason, exc.__context__))


@dataclass(frozen=True)
class NodeAddress:
    """Where a node is and the key it expects."""

    url: str
    key: str

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


class NodeClient:
    """Calls one node's routes. Safe to share between threads: each thread gets its own ``requests.Session``.

    The health loop and every job thread running a task on the node use one client, and ``requests`` doesn't promise
    that a ``Session`` (its cookie jar and connection pool) is safe to use from several threads at once.

    Args:
        address: The node's URL and key.
        session: For tests: used by every thread.
    """

    def __init__(self, address: NodeAddress, *, session: requests.Session | None = None) -> None:
        self._address = address
        self._shared_session = session
        self._local = threading.local()

    @property
    def _session(self) -> requests.Session:
        if self._shared_session is not None:
            return self._shared_session
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = requests.Session()
        return session

    @property
    def url(self) -> str:
        return self._address.base

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._address.key}", protocol.PROTOCOL_HEADER: str(protocol.NODE_PROTOCOL)}

    def _call(
        self, method: str, path: str, *, json: dict | None = None, read_timeout: float = READ_TIMEOUT_S,
        auth: bool = True, raw: bool = False,
    ) -> Any:  # fmt: skip
        # A node never compresses its answers, and requests would inflate a compressed one in memory past any cap.
        headers = {"Accept-Encoding": "identity", **(self._headers() if auth else {})}
        try:
            response = self._session.request(
                method,
                self._address.base + path,
                json=json,
                headers=headers,
                timeout=(CONNECT_TIMEOUT_S, read_timeout),
                # A redirect would carry the task (paths, mappings) to a host that isn't the node.
                allow_redirects=False,
                stream=True,
            )
        except requests.ConnectionError as exc:
            if _answer_lost(exc) and not isinstance(exc, requests.exceptions.SSLError):
                raise NodeTimeoutError(f"{self._address.base} dropped the connection before answering") from exc
            raise _unreachable(self._address.base, exc) from exc
        except requests.RequestException as exc:
            raise _unreachable(self._address.base, exc) from exc
        with response:
            return self._answer(response, path, raw=raw)

    def _read(self, response: requests.Response, limit: int) -> bytearray:
        """The body, refused once it passes ``limit`` bytes (returned as read: a copy of an artifact is ~1 GB)."""
        body = bytearray()
        try:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                body += chunk
                if len(body) > limit:
                    raise NodeProtocolError(f"{self._address.base} sent more than {limit // (1024 * 1024)} MB")
        except requests.RequestException as exc:
            # The answer had started, so the node may have acted on the request.
            raise NodeTimeoutError(f"{self._address.base} dropped the connection while answering") from exc
        return body

    def _answer(self, response: requests.Response, path: str, *, raw: bool) -> Any:
        """The node's answer to ``path``: the JSON body, or the bytes when ``raw``; anything else raises."""
        app_instance = str(response.headers.get(protocol.APP_INSTANCE_HEADER) or "")
        if app_instance:
            raise NodeProtocolError(
                f"{self._address.base} is this app's web page, not a node", app_instance=app_instance
            )
        if response.status_code == 401:
            raise NodeAuthError(f"{self._address.base} refused the key")
        if response.status_code >= 500:
            raise NodeUnreachableError(f"{self._address.base} answered HTTP {response.status_code}")
        if 300 <= response.status_code < 400:
            raise NodeProtocolError(f"{self._address.base} answered with a redirect (HTTP {response.status_code})")
        encoding = str(response.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            raise NodeProtocolError(f"{self._address.base} sent a {encoding}-compressed answer")
        if raw and response.status_code == 200:
            return self._read(response, MAX_ARTIFACT_BYTES)
        try:
            body = parse_json(self._read(response, MAX_ANSWER_BYTES))
        except ValueError as exc:
            web_page = "html" in str(response.headers.get("Content-Type") or "").lower()
            raise NodeProtocolError(
                f"{self._address.base} didn't answer as a node (HTTP {response.status_code})", web_page=web_page
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
            node = body.get("node")
            raise NodeProtocolError(message, node=node if isinstance(node, dict) else None)
        if response.status_code == 409 and kind == protocol.KIND_TASK_DELETED:
            raise NodeTaskDeletedError(message)
        if response.status_code == 400 and kind == protocol.KIND_FAILED:
            raise NodeCantStartError(message)
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
        return self._call("POST", protocol.ROUTE_GPUS_RESCAN, json={}, read_timeout=GPU_RESCAN_TIMEOUT_S)

    def access_check(self, mappings: list[protocol.PathMapping], files: list[dict]) -> dict:
        """``POST /v1/access-check`` for up to a few dozen ``{"path", "size", "mtime_ns"}`` files."""
        body = {"mapping": [m.to_json() for m in mappings], "files": files}
        return self._call("POST", protocol.ROUTE_ACCESS_CHECK, json=body, read_timeout=30.0)

    def start_task(self, spec: dict) -> str:
        """``POST /v1/tasks``; returns the task id (the spec's ``client_task_id`` when it carries one)."""
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

    def pause_task(self, task_id: str) -> None:
        """``POST /v1/tasks/<id>/pause``: the task's ``pause_check()`` reads True until it is resumed."""
        self._call("POST", f"{protocol.ROUTE_TASKS}/{task_id}/pause", json={})

    def resume_task(self, task_id: str) -> None:
        """``POST /v1/tasks/<id>/resume``."""
        self._call("POST", f"{protocol.ROUTE_TASKS}/{task_id}/resume", json={})

    def fetch_artifact(self, task_id: str) -> bytes | bytearray:
        """``GET /v1/tasks/<id>/artifact``: the task's artifact bytes (a preview's frames as a tar)."""
        return self._call(
            "GET", f"{protocol.ROUTE_TASKS}/{task_id}/artifact", read_timeout=ARTIFACT_TIMEOUT_S, raw=True
        )

    def delete_task(self, task_id: str, *, read_timeout: float = READ_TIMEOUT_S) -> None:
        """``DELETE /v1/tasks/<id>``: free the task's temp dir on the node."""
        self._call("DELETE", f"{protocol.ROUTE_TASKS}/{task_id}", read_timeout=read_timeout)

    def log(self, lines: int = 200) -> dict:
        """``GET /v1/log?lines=N``: the node's own recent log."""
        return self._call("GET", f"{protocol.ROUTE_LOG}?lines={int(lines)}")
