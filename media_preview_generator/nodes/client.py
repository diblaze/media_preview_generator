"""The app's HTTP client for one node (spec §3).

Every call raises one of the ``Node*Error`` classes below on anything but a clean answer, so callers branch on the
class, never on status codes or message text. The key is sent only in the Authorization header and never logged.
"""

from __future__ import annotations

import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from json import loads as parse_json
from typing import Any
from urllib.parse import urlsplit

import requests
from urllib3.exceptions import HTTPError, NameResolutionError, NewConnectionError, ProtocolError, ReadTimeoutError
from urllib3.response import BaseHTTPResponse

from . import protocol

# Connect fast (a LAN node either answers or it doesn't); read long enough for a busy node to answer a poll.
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 10.0
# The read timeout is the longest wait for the next byte, so an answer sent a byte at a time never ran out of it. Each
# answer must arrive whole within the connect and read timeouts together, counted from when its headers arrive (a slow
# start doesn't take the body's time): what a node that sends nothing gets. Its largest real answer (a poll's 1.3 MB) takes 0.1 s on a 100 Mbit link.
# /v1/info never waits on GPU detection, so a node that doesn't answer it this fast is stuck; three tries of it then
# call a hung node offline within a minute, however slowly it sends, while the files it dropped are held back from it.
INFO_TIMEOUT_S = 10.0
# A rescan runs GPU detection while the request waits (measured 0.7-1.3 s; a slow Vulkan probe can take longer).
GPU_RESCAN_TIMEOUT_S = 60.0
# The longest wait for the next byte of a task's frames. The node sends the tar it already wrote, so bytes start at
# once and keep coming however slow the link: this long with none is a stalled link. A job's cancel, or the app's
# stop, waits for a stalled download no longer than this (a stalled poll is given up on within ~30 s too).
ARTIFACT_TIMEOUT_S = 30.0
# The longest wait for the artifact's headers: a busy node answers a queued request once the earlier ones finish, and
# the finished work isn't redone if this gives up first.
ARTIFACT_HEADERS_TIMEOUT_S = 120.0
# The longest plausible preview (a 4-hour film at the 1 s interval: 14,400 frames of tens of KB each) is well under
# this; a node sending more is refused rather than held in memory.
MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024
# The whole artifact must arrive within this, from its headers on: MAX_ARTIFACT_BYTES does at 1.2 MB/s (10 Mbit/s).
ARTIFACT_DEADLINE_S = 15 * 60.0
# JSON parses into ~25x its size here (each 3-byte ``[],`` is a 64-byte list), so each answer gets a cap that fits it.
# Most answers are one small object: health 164 bytes and info under 1 KB measured, a few KB with many GPUs.
MAX_ANSWER_BYTES = 1024 * 1024
# A poll carries up to 2000 log lines and the task's result; the node's log 2000 lines; an access check 200 files.
# Measured: 2000 log lines with a traceback each 1.3 MB, the largest credits result of 43,144 real files 235 KB, an
# access check of 200 files 49 KB. The costliest 8 MB of JSON parses into ~220 MB.
MAX_LIST_ANSWER_BYTES = 8 * 1024 * 1024


class NodeError(Exception):
    """Base class for every node client failure."""


# Why nothing answered (``NodeUnreachableError.reason``), so a caller can say what to check.
UNREACHABLE_REFUSED = "refused"
UNREACHABLE_TIMEOUT = "timeout"
UNREACHABLE_DNS = "dns"
UNREACHABLE_TLS = "tls"
# It answered over https, but this app refused its certificate (self-signed, a private authority, another name).
UNREACHABLE_TLS_CERTIFICATE = "tls_certificate"
# It answered, but holds this app's address back after too many wrong keys (429).
UNREACHABLE_RATE_LIMITED = "rate_limited"


def unreachable_advice(
    reason: str, url: str, name: str = "It", *, was_seen: bool = False, connected: bool = False
) -> str:
    """What to check when a node didn't answer, in plain words: Add node and the node's card both say this.

    Args:
        reason: One of the ``UNREACHABLE_*`` codes, or "" when it isn't known.
        url: The address that was tried.
        name: Who starts a sentence about the node: "It" in Add node, the node's name on its card.
        was_seen: The node answered at this address before, so a name that no longer resolves more likely means it
            stopped or was renamed than that the name was typed wrong.
        connected: The connection was made but no answer came (``NodeTimeoutError``). Over http that is a stuck node,
            not a firewall; over https it is what a node without an https proxy in front looks like.

    Returns:
        One or two sentences.
    """
    https = url.lower().startswith("https:")
    if connected and not https:
        return f"{name} accepts connections but doesn't answer. It may be stuck or overloaded: restart it."
    if reason == UNREACHABLE_DNS:
        host = urlsplit(url).hostname or "that machine"
        if was_seen:
            # Docker's DNS drops a stopped container's name from a shared network.
            return f"Can't find {host}. {name} may be stopped or renamed. Check it is running, or use its IP address."
        return f"Can't find {host}. Check the name, or use the node's IP address."
    if reason == UNREACHABLE_TLS or (reason == UNREACHABLE_TIMEOUT and https):
        # A node without an https proxy in front never answers the TLS handshake: https to it times out.
        start = f"{name} doesn't answer over https" if reason == UNREACHABLE_TLS else "No answer over https in time"
        return f"{start}. A node speaks http unless you put it behind an https proxy: use http:// instead."
    if reason == UNREACHABLE_TLS_CERTIFICATE:
        return (
            f"{name} answers over https, but this app doesn't trust its certificate (self-signed, or from your own "
            "certificate authority). Let this app trust it with REQUESTS_CA_BUNDLE (see the guide), or use http://."
        )
    if reason == UNREACHABLE_REFUSED:
        return (
            "Nothing is listening on that port. Check the port (9494 unless you set NODE_PORT), and that the node "
            "is running."
        )
    if reason == UNREACHABLE_TIMEOUT:
        return (
            "No answer in time. Check the address, that the node is running, and that a firewall lets this app "
            "reach it."
        )
    if reason == UNREACHABLE_RATE_LIMITED:
        return (
            "The node is holding back requests from this address after too many wrong keys. Wait a minute and try "
            "again. If it keeps happening, something else reaching the node from the same address (or through the "
            "same proxy) is using an old key."
        )
    return "Nothing answered at this address. Check the address and port, and that the node is running."


class NodeUnreachableError(NodeError):
    """No answer: connection refused, timed out, DNS, TLS, or a 5xx from something in between.

    Args:
        message: What went wrong, in plain words.
        reason: One of the ``UNREACHABLE_*`` codes, or "" when it isn't known.
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class NodeStoppingError(NodeUnreachableError):
    """The node is stopping (503 ``node_stopping``): it took nothing, and is back once it has restarted."""


class NodeRateLimitedError(NodeUnreachableError):
    """The node asks this app's address to wait (429): too many wrong keys came from it, maybe from something else
    sharing it. The key wasn't compared and nothing ran, so this is a wait, not a refused key."""


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
    started with (a GPU rescan or an access check on a node without the worker role); or it hasn't what was asked for
    (404, kind ``request``: the artifact of a task that made none)."""


class NodeCantStartError(NodeError):
    """The node couldn't take the task (400, kind ``failed``: its temp folder is full or can't be written). Nothing
    ran, so the file can go to another worker."""


class NodeTaskDeletedError(NodeError):
    """The node refused a start whose task id this app had already deleted (409 ``task_deleted``): nothing ran."""


def _unreachable(base: str, exc: requests.RequestException) -> NodeUnreachableError:
    """A request that got no answer, saying why in plain words (never the exception's class name)."""
    cause = getattr(exc.args[0], "reason", None) if exc.args else None
    if isinstance(exc, requests.exceptions.SSLError) and _certificate_refused(cause):
        return NodeUnreachableError(
            f"{base} answered over https, but its certificate isn't trusted by this app",
            reason=UNREACHABLE_TLS_CERTIFICATE,
        )
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


def _certificate_refused(cause: object) -> bool:
    """Whether a TLS failure was this app refusing the certificate, so the address does speak https.

    urllib3 wraps the handshake's ``ssl.SSLCertVerificationError`` in its own ``SSLError``.
    """
    return any(isinstance(error, ssl.SSLCertVerificationError) for error in (cause, *getattr(cause, "args", ())))


def _answer_lost(exc: requests.ConnectionError) -> bool:
    """Whether the connection dropped after it was made (closed or reset mid-request), so the node may have the
    request; a connection never made (refused, DNS) reaches requests as ``MaxRetryError`` instead."""
    reason = exc.args[0] if exc.args else None
    return any(isinstance(cause, ProtocolError | ConnectionResetError) for cause in (reason, exc.__context__))


def _set_body_timeout(response: Any, seconds: float) -> None:
    """Make the wait for the next byte of the body ``seconds`` (the request's timeout covered the headers).

    urllib3 keeps the connection of a streamed answer on ``raw``; a stand-in answer has none, and keeps its timeout.
    """
    sock = getattr(getattr(getattr(response, "raw", None), "_connection", None), "sock", None)
    if sock is not None:
        sock.settimeout(seconds)


class AnswerTooSlowError(requests.Timeout):
    """An answer that didn't arrive whole by its deadline (``answer_pieces``)."""


def answer_pieces(response: Any, chunk_bytes: int, deadline: float) -> Iterator[bytes]:
    """A streamed answer's body as it arrives, given up on once ``deadline`` (``time.monotonic()``) has passed.

    ``iter_content`` waits for a whole chunk, which an answer sent a byte at a time never fills; urllib3's ``read1``
    hands over what one read of the socket brought, so the answer is given up on at most one read (one read timeout)
    past the deadline.

    Args:
        response: A ``requests`` answer, opened with ``stream=True``.
        chunk_bytes: The most bytes one piece holds.
        deadline: When to give up.

    Raises:
        AnswerTooSlowError: The deadline passed before the answer ended.
        requests.RequestException: The connection timed out, dropped or broke mid-answer.
    """
    raw = getattr(response, "raw", None)
    if isinstance(raw, BaseHTTPResponse):
        pieces: Iterator[bytes] = iter(partial(raw.read1, chunk_bytes), b"")
    else:
        pieces = iter(response.iter_content(chunk_size=chunk_bytes))  # a test's stand-in answer
    while True:
        try:
            piece = next(pieces, b"")
        # read1 raises urllib3's errors, which iter_content wraps in requests' own (these, as it does).
        except ReadTimeoutError as exc:
            raise requests.ConnectionError(exc) from exc
        except HTTPError as exc:
            raise requests.exceptions.ChunkedEncodingError(exc) from exc
        if not piece:
            return
        if time.monotonic() >= deadline:
            raise AnswerTooSlowError("the answer didn't arrive whole in time")
        yield piece


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
        self, method: str, path: str, *, json: dict | None = None, read_timeout: float | None = None,
        auth: bool = True, raw: bool = False, limit: int = MAX_ANSWER_BYTES, deadline_s: float | None = None,
    ) -> Any:  # fmt: skip
        if read_timeout is None:
            read_timeout = READ_TIMEOUT_S
        # The whole body's time, from when the headers arrive: by default the connect and read timeouts together, or
        # ARTIFACT_DEADLINE_S for a raw answer (the artifact).
        if deadline_s is None:
            deadline_s = ARTIFACT_DEADLINE_S if raw else CONNECT_TIMEOUT_S + read_timeout
        # An artifact's headers wait for a busy node's queue; once its bytes flow, a stall is read_timeout long.
        headers_timeout = ARTIFACT_HEADERS_TIMEOUT_S if raw else read_timeout
        # A node never compresses its answers, and requests would inflate a compressed one in memory past any cap.
        headers = {"Accept-Encoding": "identity", **(self._headers() if auth else {})}
        try:
            response = self._session.request(
                method,
                self._address.base + path,
                json=json,
                headers=headers,
                timeout=(CONNECT_TIMEOUT_S, headers_timeout),
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
        except (HTTPError, ValueError, UnicodeError) as exc:
            # urllib3 refuses some addresses requests prepared (an empty host label: LocationParseError) before it
            # connects; such a node can never answer.
            raise NodeUnreachableError(f"{self._address.base} can't be reached: {type(exc).__name__}") from exc
        with response:
            if headers_timeout != read_timeout:
                _set_body_timeout(response, read_timeout)
            return self._answer(response, path, raw=raw, limit=limit, deadline=time.monotonic() + deadline_s)

    def _read(self, response: requests.Response, limit: int, deadline: float) -> bytearray:
        """The body, refused once it passes ``limit`` bytes (returned as read: a copy of an artifact is ~1 GB), or
        once ``deadline`` has passed."""
        body = bytearray()
        try:
            for chunk in answer_pieces(response, 1024 * 1024, deadline):
                body += chunk
                if len(body) > limit:
                    raise NodeProtocolError(f"{self._address.base} sent more than {limit // (1024 * 1024)} MB")
        # The answer had started, so the node may have acted on the request.
        except AnswerTooSlowError as exc:
            raise NodeTimeoutError(f"{self._address.base} didn't answer in time", reason=UNREACHABLE_TIMEOUT) from exc
        except requests.RequestException as exc:
            if exc.args and isinstance(exc.args[0], ReadTimeoutError):
                # It waited out its whole read timeout, like no answer at all: asking again waits as long again.
                raise NodeTimeoutError(
                    f"{self._address.base} stopped sending partway through its answer", reason=UNREACHABLE_TIMEOUT
                ) from exc
            raise NodeTimeoutError(f"{self._address.base} dropped the connection while answering") from exc
        return body

    def _error_kind(self, response: requests.Response, deadline: float) -> str:
        """The ``error.kind`` of a node's JSON answer, or "" when the answer isn't one (a proxy's error page)."""
        try:
            body = parse_json(self._read(response, MAX_ANSWER_BYTES, deadline))
        except (ValueError, RecursionError, NodeError):  # RecursionError: JSON nested deeper than the parser goes
            return ""
        error = body.get("error") if isinstance(body, dict) else None
        return str(error.get("kind") or "") if isinstance(error, dict) else ""

    def _answer(self, response: requests.Response, path: str, *, raw: bool, limit: int, deadline: float) -> Any:
        """The node's answer to ``path``: the JSON body (refused unparsed past ``limit`` bytes), or the bytes when
        ``raw``; anything else, or an answer still arriving at ``deadline``, raises."""
        app_instance = str(response.headers.get(protocol.APP_INSTANCE_HEADER) or "")
        if app_instance:
            raise NodeProtocolError(
                f"{self._address.base} is this app's web page, not a node", app_instance=app_instance
            )
        if response.status_code == 401:
            raise NodeAuthError(f"{self._address.base} refused the key")
        if response.status_code == 429:
            raise NodeRateLimitedError(
                f"{self._address.base} asks this app to wait: too many wrong keys came from here"
            )
        if response.status_code >= 500:
            if response.status_code == 503 and self._error_kind(response, deadline) == protocol.KIND_NODE_STOPPING:
                raise NodeStoppingError(f"{self._address.base} is restarting")
            raise NodeUnreachableError(f"{self._address.base} answered HTTP {response.status_code}")
        if 300 <= response.status_code < 400:
            raise NodeProtocolError(f"{self._address.base} answered with a redirect (HTTP {response.status_code})")
        encoding = str(response.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            raise NodeProtocolError(f"{self._address.base} sent a {encoding}-compressed answer")
        if raw and response.status_code == 200:
            return self._read(response, MAX_ARTIFACT_BYTES, deadline)
        try:
            body = parse_json(self._read(response, limit, deadline))
        except (ValueError, RecursionError) as exc:  # RecursionError: JSON nested deeper than Python's parser goes
            web_page = "html" in str(response.headers.get("Content-Type") or "").lower()
            raise NodeProtocolError(
                f"{self._address.base} didn't answer as a node (HTTP {response.status_code})", web_page=web_page
            ) from exc
        if not isinstance(body, dict):
            raise NodeProtocolError(f"{self._address.base} didn't answer as a node")
        raw_error = body.get("error")
        error: dict = raw_error if isinstance(raw_error, dict) else {}
        kind = error.get("kind")
        message = protocol.relayed_text(str(error.get("message") or kind or f"HTTP {response.status_code}"))
        if response.status_code == 404 and kind == protocol.KIND_UNKNOWN_TASK:
            raise NodeUnknownTaskError(message)
        if response.status_code == 404 and kind == protocol.KIND_REQUEST:
            # The node knows the route but hasn't what was asked for (the artifact of a task that made none).
            raise NodeRequestError(message)
        if response.status_code == 404:
            # A Plex helper from before nodes existed answers its own routes but 404s the node ones.
            raise NodeProtocolError(f"{self._address.base} doesn't know {path} (an older Plex database node?)")
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
        return self._call(
            "POST", protocol.ROUTE_ACCESS_CHECK, json=body, read_timeout=30.0, limit=MAX_LIST_ANSWER_BYTES
        )

    def start_task(self, spec: dict) -> str:
        """``POST /v1/tasks``; returns the task id (the spec's ``client_task_id`` when it carries one)."""
        body = self._call("POST", protocol.ROUTE_TASKS, json=spec)
        task_id = body.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise NodeProtocolError(f"{self._address.base} started a task without an id")
        return task_id

    def poll_task(self, task_id: str, *, log_from: int = 0) -> dict:
        """``GET /v1/tasks/<id>?log_from=N``: the task state with log lines from ``log_from`` on."""
        return self._call(
            "GET", f"{protocol.ROUTE_TASKS}/{task_id}?log_from={int(log_from)}", limit=MAX_LIST_ANSWER_BYTES
        )

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
        return self._call("GET", f"{protocol.ROUTE_LOG}?lines={int(lines)}", limit=MAX_LIST_ANSWER_BYTES)
