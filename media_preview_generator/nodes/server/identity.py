"""Who this node is: a stable id, a display name, and the key the app must send.

``<CONFIG_DIR>/node.json`` holds ``{"id", "key", "announced"}``, written with mode 0600. The id never changes, so the
app can tell this node from another one answering at the same address. The key the node expects is, in order: the one
passed in, ``NODE_KEY``, ``AGENT_TOKEN`` (what a Plex helper was set up with), else the generated one in node.json,
shown once in the console the first time it is the one in use (``announced`` records that it was).
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import secrets
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from ..protocol import sendable_key
from .nodelog import SECRET_EXTRA

IDENTITY_FILE = "node.json"
# The examples in the docs and compose files once carried keys with this in them; a key copied from there is public.
PLACEHOLDER_MARK = "change-me"
# A key set with NODE_KEY is new, so it must be long enough not to be guessed. A Plex helper's AGENT_TOKEN predates the
# rule: it keeps working whatever its length (with a warning), so an upgrade never locks the app out.
MIN_KEY_LENGTH = 16
MAKE_A_KEY = "openssl rand -hex 24"
# Wrong keys one caller address may send at once, then one more every WRONG_KEY_REFILL_S: guessing even a short
# AGENT_TOKEN runs at 20 keys a minute instead of hundreds a second. The app sends the right key and spends none, but
# shares an address's limit with anything else there (NAT, a proxy, an old copy with an old key): while the address is
# held back its key isn't compared at all, so the answer is 429 with Retry-After -- a wait, never "wrong key".
WRONG_KEY_BURST = 20
WRONG_KEY_REFILL_S = 3.0
# An IPv6 machine is usually handed a whole /64 and can use a new address in it for every request, and the other
# machines of its network share that /64 (or its ULA or link-local one). So each address has its own limit, and its /64
# as a whole one this many times looser (burst and refill): a device with an old key holds back only itself, and one
# taking a new address for every key guesses this many times faster than one address, no more.
WRONG_KEY_NETWORK_FACTOR = 10
# Addresses remembered at most; past it, those owing nothing are forgotten, then the longest quiet of those not held
# back. One held back is never forgotten: wrong keys from other addresses would otherwise give it its whole burst again.
WRONG_KEY_ADDRESSES_MAX = 4096
_WRONG_KEYS_EXTENSION = "mpg_node_wrong_keys"


def caller_key(address: str) -> str:
    """What one caller's wrong keys and connections are counted by: its address.

    Args:
        address: The caller's address as the server saw it.

    Returns:
        The address (an IPv4-mapped IPv6 address as plain IPv4, IPv6 in its short form), or ``address`` unchanged when
        it isn't an IP address.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ip)


def caller_network(address: str) -> str | None:
    """The /64 an IPv6 caller's address is in, counted as a whole too (:data:`WRONG_KEY_NETWORK_FACTOR`).

    Args:
        address: The caller's address as the server saw it.

    Returns:
        The /64 network, or None for an IPv4 (or IPv4-mapped) address and anything that isn't an IP address.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if not isinstance(ip, ipaddress.IPv6Address) or ip.ipv4_mapped is not None:
        return None
    return str(ipaddress.IPv6Network((int(ip) >> 64 << 64, 64)))


class WrongKeyLimit:
    """The wrong keys each caller address (:func:`caller_key`) and IPv6 /64 (:func:`caller_network`) sent lately, so a
    node's guards stop comparing keys from one that sends too many and tell it to wait (thread-safe).

    Args:
        burst: Wrong keys an address may send at once.
        refill_s: How long until it may send one more.
        clock: For tests.
    """

    def __init__(
        self,
        *,
        burst: int = WRONG_KEY_BURST,
        refill_s: float = WRONG_KEY_REFILL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst, self._refill_s, self._clock = burst, refill_s, clock
        # Address or /64 -> (wrong keys not yet refilled, as of when).
        self._owed: dict[str, tuple[float, float]] = {}
        # Those already named in the log for this run of wrong keys, so one that keeps sending them is named once.
        self._logged: set[str] = set()
        self._lock = threading.Lock()

    def _limits(self, counted: str) -> tuple[float, float]:
        """``(burst, refill_s)`` for an address, or the looser ones of a /64 (its key holds the "/")."""
        if "/" in counted:
            return self._burst * WRONG_KEY_NETWORK_FACTOR, self._refill_s / WRONG_KEY_NETWORK_FACTOR
        return self._burst, self._refill_s

    def _owing(self, counted: str, now: float) -> float:
        owed, since = self._owed.get(counted, (0.0, now))
        return max(0.0, owed - (now - since) / self._limits(counted)[1])

    def _wait_s(self, counted: str, now: float) -> float:
        burst, refill_s = self._limits(counted)
        return max(0.0, self._owing(counted, now) - (burst - 1)) * refill_s

    @staticmethod
    def _counted(address: str) -> list[str]:
        network = caller_network(address)
        return [caller_key(address), *([network] if network else [])]

    def blocked_for(self, address: str) -> int:
        """Whole seconds until ``address`` may send a key again; 0 when it may now.

        While it is above 0 the guards answer 429 without comparing the key, so a guesser learns nothing.
        """
        with self._lock:
            now = self._clock()
            wait_s = max(self._wait_s(counted, now) for counted in self._counted(address))
        return max(1, math.ceil(wait_s)) if wait_s > 0 else 0

    def note_wrong(self, address: str) -> None:
        """Count one wrong (or missing) key from ``address``; the first time it is held back, say so in the log."""
        named: list[str] = []
        with self._lock:
            now = self._clock()
            for counted in self._counted(address):
                before = self._owing(counted, now)
                if not before:
                    self._logged.discard(counted)
                self._owed[counted] = (before + 1, now)
                if before + 1 > self._limits(counted)[0] - 1 and counted not in self._logged:
                    self._logged.add(counted)
                    named.append(counted)
            if len(self._owed) > WRONG_KEY_ADDRESSES_MAX:
                owing = {c: self._owing(c, now) for c in self._owed}
                held = {c: v for c, v in self._owed.items() if owing[c] > self._limits(c)[0] - 1}
                room = max(0, WRONG_KEY_ADDRESSES_MAX // 2 - len(held))
                rest = sorted(
                    ((c, v) for c, v in self._owed.items() if 0 < owing[c] <= self._limits(c)[0] - 1),
                    key=lambda item: item[1][1],
                )
                self._owed = {**dict(rest[-room:] if room else []), **held}
                self._logged &= set(self._owed)
        for counted in named:
            logger.warning(
                "Too many wrong keys came from {}, so this node asks everything there to wait, even with the right "
                "key. Something there has an old key: give it the right one, or stop it.",
                f"addresses in {counted}" if "/" in counted else counted,
            )


def wrong_key_limit(app: Any) -> WrongKeyLimit:
    """The node app's one :class:`WrongKeyLimit`, shared by the node routes' guard and the Plex database role's."""
    return app.extensions.setdefault(_WRONG_KEYS_EXTENSION, WrongKeyLimit())


@dataclass(frozen=True)
class NodeIdentity:
    """This node's id, name and expected key (``key_source`` says where the key came from, never the key)."""

    id: str
    name: str
    key: str
    key_source: str
    key_generated: bool

    def __repr__(self) -> str:
        return f"NodeIdentity(id={self.id!r}, name={self.name!r}, key_source={self.key_source!r})"


def default_config_dir() -> str:
    """``CONFIG_DIR``, or ``/config`` (the image's volume)."""
    return os.environ.get("CONFIG_DIR", "").strip() or "/config"


def _check_key(key: str, source: str, *, min_length: int = 0) -> str:
    # The app refuses to store a key outside printable ASCII without spaces, so such a key could never match one the
    # app sends: every request would 401 and look like a typo on the app's side. Said once, at start.
    if not sendable_key(key):
        raise SystemExit(
            f"{source} must be printable ASCII with no spaces: the app can't send a key with any other character."
        )
    if PLACEHOLDER_MARK in key.lower():
        raise SystemExit(
            f"{source} is the example key from the docs, so anyone can read it. Remove {source} and the node makes "
            f"a key of its own (shown once in its log), or set a random one, made with: {MAKE_A_KEY}"
        )
    if len(key) < min_length:
        raise SystemExit(
            f"{source} is too short to be safe ({len(key)} characters; use at least {min_length}). Remove it and the "
            f"node makes a key of its own (shown once in its log), or set a random one, made with: {MAKE_A_KEY}"
        )
    return key


def _write_private(path: str, data: dict[str, Any]) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unreadable(path: str, exc: OSError) -> str:
    """Why ``node.json`` can't be read, and the fix (a changed PUID/PGID is the usual cause)."""
    if not isinstance(exc, PermissionError):
        return f"Can't read {path} ({exc.strerror or type(exc).__name__}); fix it and restart."
    running = getattr(os, "geteuid", lambda: None)()  # Windows has no uids
    try:
        owner: int | None = os.stat(path).st_uid
    except OSError:
        owner = None
    whose = f"it belongs to user {owner} and this node runs as user {running}" if None not in (owner, running) else ""
    return (
        f"Can't read {path}{f' ({whose})' if whose else ''}. Set PUID/PGID back to the user that made it, or give it "
        "to this container's user, and restart."
    )


def _load_or_create(config_dir: str) -> tuple[dict[str, Any], bool]:
    path = os.path.join(config_dir, IDENTITY_FILE)
    # Regenerating would silently give the node a new id and key, and every app that added it would refuse it; so the
    # node stops, and says what deleting the file costs.
    rekeys = "Deleting it gives the node a new id and key, and every app that added it has to add it again."
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        stored = None
    except OSError as exc:
        raise SystemExit(_unreadable(path, exc)) from exc
    except ValueError as exc:
        raise SystemExit(f"{path} isn't valid JSON; restore it from a backup and restart. {rekeys}") from exc
    if stored is not None:
        if (
            not isinstance(stored, dict)
            or not isinstance(stored.get("id"), str)
            or not stored["id"].strip()
            or not isinstance(stored.get("key"), str)
        ):
            raise SystemExit(f"{path} isn't a node identity file; restore it from a backup and restart. {rekeys}")
        return {"id": stored["id"], "key": stored["key"], "announced": stored.get("announced") is True}, False
    data = {"id": uuid.uuid4().hex, "key": secrets.token_urlsafe(32), "announced": False}
    try:
        os.makedirs(config_dir, exist_ok=True)
        _write_private(path, data)
    except OSError as exc:
        raise SystemExit(
            f"Can't write {path} ({exc.strerror or type(exc).__name__}); make the config folder writable for this "
            "container's user (PUID/PGID) and restart."
        ) from exc
    return data, True


def _announce_key(key: str, config_dir: str) -> None:
    lines = [
        "This node made its key (shown once, now; it is kept in " + os.path.join(config_dir, IDENTITY_FILE) + "):",
        "",
        f"    {key}",
        "",
        "Paste it into the app's Add node dialog (Settings -> Nodes), or set NODE_KEY to a key of your own.",
    ]
    width = max(len(line) for line in lines) + 2
    box = ["+" + "-" * width + "+", *(f"| {line.ljust(width - 2)} |" for line in lines), "+" + "-" * width + "+"]
    logger.bind(**{SECRET_EXTRA: True}).warning("\n" + "\n".join(box))


def _mark_announced(config_dir: str, stored: dict[str, Any]) -> None:
    path = os.path.join(config_dir, IDENTITY_FILE)
    try:
        _write_private(path, {**stored, "announced": True})
    except OSError as exc:
        # The key still works; it is only shown again at the next start.
        logger.warning("Couldn't note in {} that its key was shown ({}); it is shown again next start", path, exc)


def load_identity(config_dir: str | None = None, *, key: str | None = None) -> NodeIdentity:
    """Read (or create on first start) this node's identity.

    Args:
        config_dir: Where ``node.json`` lives (default :func:`default_config_dir`).
        key: An explicit key; wins over the environment and node.json.

    Returns:
        The identity.

    Raises:
        SystemExit: node.json can't be read or written, or the key isn't printable ASCII, is a docs example, or
            (from ``NODE_KEY``) is shorter than :data:`MIN_KEY_LENGTH`.
    """
    folder = config_dir or default_config_dir()
    stored, created = _load_or_create(folder)
    name = os.environ.get("NODE_NAME", "").strip() or socket.gethostname()
    if key is not None:
        chosen, source = _check_key(key.strip(), "The node key"), "argument"
    elif os.environ.get("NODE_KEY", "").strip():
        chosen = _check_key(os.environ["NODE_KEY"].strip(), "NODE_KEY", min_length=MIN_KEY_LENGTH)
        source = "NODE_KEY"
    elif os.environ.get("AGENT_TOKEN", "").strip():
        chosen, source = _check_key(os.environ["AGENT_TOKEN"].strip(), "AGENT_TOKEN"), "AGENT_TOKEN"
        if len(chosen) < MIN_KEY_LENGTH:
            logger.warning(
                "AGENT_TOKEN is only {} characters, short enough to be guessed. Set NODE_KEY to a longer one "
                "(made with: {}) and paste it into the app's node too.",
                len(chosen),
                MAKE_A_KEY,
            )
    else:
        chosen, source = _check_key(stored["key"], os.path.join(folder, IDENTITY_FILE) + "'s key"), IDENTITY_FILE
        # Not "when created": a node first started with NODE_KEY and later without it runs on this key from then on.
        if not stored["announced"]:
            _announce_key(chosen, folder)
            _mark_announced(folder, stored)
    return NodeIdentity(
        id=stored["id"], name=name, key=chosen, key_source=source, key_generated=created and source == IDENTITY_FILE
    )
