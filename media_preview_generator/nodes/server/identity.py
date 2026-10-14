"""Who this node is: a stable id, a display name, and the key the app must send.

``<CONFIG_DIR>/node.json`` holds ``{"id", "key"}``, written once with mode 0600. The id never changes, so the app
can tell this node from another one answering at the same address. The key the node expects is, in order: the one
passed in, ``NODE_KEY``, ``AGENT_TOKEN`` (what a Plex helper was set up with), else the generated one in node.json.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import uuid
from dataclasses import dataclass

from loguru import logger

from .nodelog import SECRET_EXTRA

IDENTITY_FILE = "node.json"


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


def _check_key(key: str, source: str) -> str:
    # The app refuses to store a key that isn't printable ASCII, so a key with anything else in it could never match
    # one the app sends: every request would 401 and look like a typo on the app's side. Said once, at start.
    if not key or not key.isascii() or not key.isprintable():
        raise SystemExit(f"{source} must be plain printable ASCII: the app can't send a key with any other character.")
    return key


def _write_private(path: str, data: dict[str, str]) -> None:
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


def _load_or_create(config_dir: str) -> tuple[dict[str, str], bool]:
    path = os.path.join(config_dir, IDENTITY_FILE)
    try:
        with open(path, encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        stored = None
    except (OSError, ValueError) as exc:
        # Regenerating would silently give the node a new id and key, and every app that added it would refuse it.
        raise SystemExit(f"Can't read {path} ({type(exc).__name__}); fix or remove it and restart.") from exc
    if stored is not None:
        if (
            not isinstance(stored, dict)
            or not isinstance(stored.get("id"), str)
            or not isinstance(stored.get("key"), str)
        ):
            raise SystemExit(f"{path} isn't a node identity file; fix or remove it and restart.")
        return {"id": stored["id"], "key": stored["key"]}, False
    data = {"id": uuid.uuid4().hex, "key": secrets.token_urlsafe(32)}
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


def load_identity(config_dir: str | None = None, *, key: str | None = None) -> NodeIdentity:
    """Read (or create on first start) this node's identity.

    Args:
        config_dir: Where ``node.json`` lives (default :func:`default_config_dir`).
        key: An explicit key; wins over the environment and node.json.

    Returns:
        The identity.

    Raises:
        SystemExit: node.json can't be read or written, or the key isn't printable ASCII.
    """
    folder = config_dir or default_config_dir()
    stored, created = _load_or_create(folder)
    name = os.environ.get("NODE_NAME", "").strip() or socket.gethostname()
    if key is not None:
        chosen, source = _check_key(key.strip(), "The node key"), "argument"
    elif os.environ.get("NODE_KEY", "").strip():
        chosen, source = _check_key(os.environ["NODE_KEY"].strip(), "NODE_KEY"), "NODE_KEY"
    elif os.environ.get("AGENT_TOKEN", "").strip():
        chosen, source = _check_key(os.environ["AGENT_TOKEN"].strip(), "AGENT_TOKEN"), "AGENT_TOKEN"
    else:
        chosen, source = _check_key(stored["key"], os.path.join(folder, IDENTITY_FILE) + "'s key"), IDENTITY_FILE
        if created:
            _announce_key(chosen, folder)
    return NodeIdentity(
        id=stored["id"], name=name, key=chosen, key_source=source, key_generated=created and source == IDENTITY_FILE
    )
