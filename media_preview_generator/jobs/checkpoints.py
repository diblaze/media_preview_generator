"""Private, atomic continuations for jobs parked outside worker availability.

Only unfinished work and aggregate accounting are saved. Clients, registries,
worker objects and cached Plex bundle metadata never cross this boundary.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..processing.types import ProcessableItem

_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _validate_accounting(snapshot: dict) -> None:
    """Reject corrupt accounting rather than prematurely completing remaining work."""
    state = snapshot.get("state")
    if not isinstance(state, dict):
        raise ValueError("Invalid checkpoint accounting")
    for key in ("successful", "failed", "cpu_fallback_files", "total_items"):
        value = state.get(key, 0)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid checkpoint counter")
    if "total_items" in state and state["total_items"] != (
        len(snapshot["items"]) + state.get("successful", 0) + state.get("failed", 0)
    ):
        raise ValueError("Checkpoint work and counters disagree")
    outcomes = state.get("outcome_counts", {})
    if not isinstance(outcomes, dict) or any(type(v) is not int or v < 0 for v in outcomes.values()):
        raise ValueError("Invalid checkpoint outcomes")
    failed = state.get("failed_paths", [])
    if not isinstance(failed, list) or any(not isinstance(path, str) for path in failed):
        raise ValueError("Invalid checkpoint failed work")
    if not isinstance(state.get("publishers_aggregate", {}), dict):
        raise ValueError("Invalid checkpoint publisher accounting")


def item_descriptor(item: ProcessableItem) -> dict[str, Any]:
    """Keep a work item's identity while discarding enumeration-time caches."""
    return {
        "canonical_path": item.canonical_path,
        "server_id": item.server_id,
        "item_id_by_server": dict(item.item_id_by_server),
        "title": item.title,
        "library_id": item.library_id,
    }


def checkpoint_items(snapshot: dict) -> list[ProcessableItem]:
    """Validate and reconstruct unfinished work, without stale bundle metadata."""
    raw_items = snapshot.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Checkpoint items must be a list")
    items = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("Invalid checkpoint work item")
        path = raw.get("canonical_path")
        server_id = raw.get("server_id")
        ids = raw.get("item_id_by_server", {})
        if not isinstance(path, str) or not os.path.isabs(path) or not isinstance(server_id, str):
            raise ValueError("Invalid checkpoint work identity")
        if not isinstance(ids, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in ids.items()):
            raise ValueError("Invalid checkpoint server identifiers")
        title = raw.get("title", "")
        library = raw.get("library_id")
        if not isinstance(title, str) or (library is not None and not isinstance(library, str)):
            raise ValueError("Invalid checkpoint work metadata")
        items.append(ProcessableItem(path, server_id, dict(ids), title, library))
    return items


def _directory(config_dir: str | Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not _SAFE_ID.fullmatch(job_id):
        raise ValueError("Invalid checkpoint job ID")
    return Path(config_dir) / "job_checkpoints"


def _path(config_dir: str | Path, job_id: str, reference: str) -> Path:
    directory = _directory(config_dir, job_id)
    if not isinstance(reference, str) or not re.fullmatch(re.escape(job_id) + r"\.[0-9a-f]{32}\.json", reference):
        raise ValueError("Invalid checkpoint reference")
    return directory / reference


def write_checkpoint(config_dir: str | Path, job_id: str, snapshot: dict, *, bookkeeping: dict | None = None) -> str:
    """Durably save a new continuation; caller commits its reference before detaching.

    Failure leaves the caller's live tracker intact. Unique references ensure a
    later attempt cannot overwrite an earlier committed continuation.
    """
    checkpoint_items(snapshot)
    _validate_accounting(snapshot)
    directory = _directory(config_dir, job_id)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    reference = f"{job_id}.{uuid.uuid4().hex}.json"
    payload = {**snapshot, "version": 1, "job_id": job_id, "bookkeeping": bookkeeping or {}}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as output:
            temporary = output.name
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / reference)
        temporary = None
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return reference


def read_checkpoint(config_dir: str | Path, job_id: str, reference: str) -> dict:
    """Read only the named job's private continuation and validate its identity."""
    path = _path(config_dir, job_id, reference)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as source:
        payload = json.load(source)
    if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("job_id") != job_id:
        raise ValueError("Checkpoint belongs to another job or format")
    checkpoint_items(payload)
    _validate_accounting(payload)
    if not isinstance(payload.get("state"), dict) or not isinstance(payload.get("bookkeeping", {}), dict):
        raise ValueError("Invalid checkpoint accounting")
    return payload


def delete_checkpoint(config_dir: str | Path, job_id: str, reference: str) -> None:
    """Remove a continuation only after its job no longer needs it."""
    _path(config_dir, job_id, reference).unlink(missing_ok=True)
