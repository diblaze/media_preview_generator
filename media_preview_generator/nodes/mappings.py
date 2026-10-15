"""Test access: can a node see the app's media through its folder mappings (spec §10.4)?

The app samples a few real video files from every enabled library, as the app sees them, and asks the node to find
each one through the mappings (same size and ``mtime_ns``, so a different file at the same path doesn't pass). When
files are missing, the folder listing the node sends back is used to suggest a corrected mapping row.
"""

from __future__ import annotations

import os
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .client import NodeClient
from .protocol import PathMapping, map_app_path

SAMPLES_PER_ROOT = 3
# Directory entries looked at per library folder before giving up: a library whose first 500 entries hold no video
# reports "no video files found" rather than walking a whole NAS.
WALK_LIMIT = 500
# Files per access-check request; the node checks each one on disk.
CHECK_BATCH = 30
# The longest Test access spends reading this app's own library folders. A stalled network mount blocks a directory
# read in the kernel where no timeout reaches it, so each library is read on its own thread and one that isn't done
# by then is reported as timed out; the request answers with what the others found.
SAMPLE_BUDGET_S = 20.0


def _video_extensions() -> frozenset[str]:
    from ..plex_client import VIDEO_EXTENSIONS

    return VIDEO_EXTENSIONS


@dataclass(frozen=True)
class LibraryRoot:
    """One library folder as this app sees it."""

    server: str
    server_id: str
    library: str
    library_id: str
    app_root: str
    # The server's folder this one was mapped from: a multi-disk setup maps one to several of this app's folders.
    remote: str = ""


def library_roots(servers: list[dict]) -> list[LibraryRoot]:
    """Every enabled library folder of every enabled server, resolved to this app's own paths.

    Args:
        servers: The stored ``media_servers`` list.

    Returns:
        One entry per distinct (server, library, folder), in settings order.
    """
    from ..servers.ownership import apply_path_mappings
    from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict

    out: list[LibraryRoot] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in servers or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        try:
            cfg = server_config_from_dict(entry)
        except UnsupportedServerTypeError:
            continue
        for lib in cfg.libraries:
            if not lib.enabled:
                continue
            for remote in lib.remote_paths:
                for local in apply_path_mappings(remote, cfg.path_mappings):
                    root = local.rstrip("/") or "/"
                    key = (cfg.id, lib.id, root)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(LibraryRoot(cfg.name, cfg.id, lib.name, lib.id, root, remote))
    return out


@dataclass
class _Sampling:
    """One library folder's sampling, shared with the thread doing it (which may still be stuck in a read)."""

    found: list[dict] = field(default_factory=list)
    folder: str = ""
    timed_out: bool = False
    opened: bool = False
    done: threading.Event = field(default_factory=threading.Event)


def sample_files(root: str, *, limit: int = SAMPLES_PER_ROOT, walk_limit: int = WALK_LIMIT) -> list[dict]:
    """Up to ``limit`` video files under ``root``, breadth first, with their size and ``mtime_ns``.

    Args:
        root: A folder as this app sees it.
        limit: Files wanted.
        walk_limit: Directory entries looked at before stopping.

    Returns:
        ``[{"path", "size", "mtime_ns"}]``; empty when the folder is missing or holds no video in reach.
    """
    sampling = _Sampling()
    _sample_into(sampling, root, limit=limit, walk_limit=walk_limit, deadline=None)
    return sampling.found


def _sample_into(sampling: _Sampling, root: str, *, limit: int, walk_limit: int, deadline: float | None) -> None:
    """:func:`sample_files` into ``sampling``, stopping (``timed_out``) at ``deadline`` between directory reads."""
    extensions = _video_extensions()
    found = sampling.found
    seen = 0
    queue: deque[str] = deque([root])
    # Libraries are often built from symlinked show folders, so links are followed; a link back into a folder already
    # walked through a link is skipped, which ends a loop after one turn (walk_limit bounds the rest).
    linked: set[str] = {os.path.realpath(root)}
    while queue and len(found) < limit and seen < walk_limit:
        if deadline is not None and time.monotonic() >= deadline:
            sampling.timed_out = True
            return
        folder = queue.popleft()
        sampling.folder = folder
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        sampling.opened = True
        for entry in entries:
            seen += 1
            if seen > walk_limit or len(found) >= limit:
                break
            try:
                if entry.is_dir():
                    if entry.name.startswith("."):
                        continue
                    if entry.is_symlink():
                        real = os.path.realpath(entry.path)
                        if real in linked:
                            continue
                        linked.add(real)
                    queue.append(entry.path)
                    continue
                if os.path.splitext(entry.name)[1].lower() not in extensions:
                    continue
                stat = entry.stat()
            except OSError:
                continue
            found.append({"path": entry.path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})


def _sample_folders(folders: list[str], budget_s: float, *, limit: int) -> list[_Sampling]:
    """Sample every folder at once, giving up on the ones not done within ``budget_s``; one result per folder.

    Daemon threads, not a pool: a thread stuck in a read on a stalled mount must neither hold up the answer nor the
    app's exit.
    """
    deadline = time.monotonic() + budget_s
    samplings: list[_Sampling] = []
    for folder in folders:
        sampling = _Sampling()
        samplings.append(sampling)

        def run(sampling: _Sampling = sampling, folder: str = folder) -> None:
            try:
                _sample_into(sampling, folder, limit=limit, walk_limit=WALK_LIMIT, deadline=deadline)
            finally:
                sampling.done.set()

        threading.Thread(target=run, name="test-access-sample", daemon=True).start()
    for sampling in samplings:
        if not sampling.done.wait(max(0.0, deadline - time.monotonic())):
            sampling.timed_out = True
    return samplings


def _sample_roots(roots: list[LibraryRoot], budget_s: float) -> dict[LibraryRoot, _Sampling]:
    """:func:`_sample_folders` for every library folder."""
    samplings = _sample_folders([root.app_root for root in roots], budget_s, limit=SAMPLES_PER_ROOT)
    return dict(zip(roots, samplings, strict=True))


def _covering_row(app_root: str, mappings: list[PathMapping]) -> PathMapping | None:
    best: PathMapping | None = None
    for row in mappings:
        prefix = row.app_prefix.rstrip("/") or "/"
        covers = app_root == prefix or app_root.startswith(prefix.rstrip("/") + "/")
        if covers and (best is None or len(prefix) > len(best.app_prefix.rstrip("/"))):
            best = row
    return best


def _node_sep(path: str) -> str:
    return "\\" if "\\" in path or (len(path) >= 2 and path[1] == ":") else "/"


def _join(folder: str, name: str) -> str:
    sep = _node_sep(folder)
    return folder if not name else (folder if folder.endswith(sep) else folder + sep) + name


def _parent(path: str, levels: int) -> str:
    sep = _node_sep(path)
    for _ in range(levels):
        trimmed = path.rstrip(sep)
        cut = trimmed.rfind(sep)
        if cut <= 0:
            return trimmed[: cut + 1] if cut == 0 else trimmed
        path = trimmed[:cut]
        if len(path) == 2 and path[1] == ":":
            path += sep
    return path


def _caseless(name: str) -> str:
    """``name`` for a case- and Unicode-form-insensitive compare (macOS and SMB often store names decomposed)."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _closest_name(wanted: str, names: list) -> str | None:
    """The name in ``names`` that is ``wanted``: exactly, then ignoring case, then ignoring case and Unicode form."""
    candidates = [n for n in names if isinstance(n, str)]
    for key in (lambda n: n, str.casefold, _caseless):
        target = key(wanted)
        match = next((n for n in candidates if key(n) == target), None)
        if match is not None:
            return match
    return None


def suggest_row(app_root: str, mappings: list[PathMapping], nearby: object) -> dict | None:
    """A corrected ``node_prefix`` for the row covering ``app_root``, from the node's folder listings.

    When a listing on the node holds a folder named like the app root's last component (ignoring case and Unicode
    form), that folder is where the node sees the root, so the row's ``node_prefix`` moves to match.

    Args:
        app_root: The library folder as this app sees it.
        mappings: The rows tested.
        nearby: The node's ``{node_dir: [names]}`` listings.

    Returns:
        ``{"from", "to"}`` (the row's current and suggested ``node_prefix``), or None.
    """
    row = _covering_row(app_root, mappings)
    if row is None or not isinstance(nearby, dict):
        return None
    prefix = row.app_prefix.rstrip("/") or "/"
    rest = [part for part in app_root[len(prefix) :].split("/") if part]
    last = rest[-1] if rest else app_root.rstrip("/").rsplit("/", 1)[-1]
    if not last:
        return None
    for node_dir, names in nearby.items():
        if not isinstance(node_dir, str) or not isinstance(names, list):
            continue
        match = _closest_name(last, names)
        if match is None:
            continue
        seen_root = _join(node_dir, match)
        # The row maps app_prefix, not app_root: step back up as many folders as app_root sits below app_prefix.
        new_prefix = _parent(seen_root, len(rest)) if rest else seen_root
        if new_prefix.rstrip("/\\") != row.node_prefix.rstrip("/\\"):
            return {"from": row.node_prefix, "to": new_prefix}
    return None


def _under(path: str, folder: str) -> bool:
    sep = _node_sep(folder)
    base = folder.rstrip(sep)
    return path == base or path.startswith(base + sep)


def _nearby_for(rows: list[dict], nearby: dict) -> dict[str, list]:
    """The node's folder listings above this library's missing files (the request's listings cover every library)."""
    missing = [r["node_path"] for r in rows if not r["ok"] and isinstance(r.get("node_path"), str)]
    return {d: names for d, names in nearby.items() if any(_under(p, d) for p in missing)}


def _check(client: NodeClient, mappings: list[PathMapping], files: list[dict]) -> tuple[dict[str, dict], dict]:
    results: dict[str, dict] = {}
    nearby: dict[str, Any] = {}
    for start in range(0, len(files), CHECK_BATCH):
        answer = client.access_check(mappings, files[start : start + CHECK_BATCH])
        for result in answer.get("results") or []:
            if isinstance(result, dict) and isinstance(result.get("path"), str):
                results[result["path"]] = result
        if isinstance(answer.get("nearby"), dict):
            nearby.update(answer["nearby"])
    return results, nearby


def run_test_access(client: NodeClient, mappings: list[PathMapping], servers: list[dict]) -> dict:
    """Sample every enabled library and ask the node to find each file through ``mappings``.

    Args:
        client: The node.
        mappings: The rows to test (saved or just edited).
        servers: The stored ``media_servers`` list.

    Returns:
        ``{"libraries": [{"server", "library", "app_root", "checked", "ok", "files": [{"path", "ok", "kind",
        "message", "node_path"}], "nearby": {node_dir: [names]}, "suggestion": {"from", "to"} | None,
        "error": str | None, "empty": bool}]}``. ``nearby`` lists what the node does see above the library's
        missing files; ``suggestion`` is the corrected ``node_prefix`` of the row covering the library; ``empty`` says this app
        opened the folder and found no video near its top. A disk of a multi-disk library that lacks the folder is
        left out while another disk holds it.

    Raises:
        NodeError: The node couldn't be asked.
    """
    roots = library_roots(servers)
    samplings = _sample_roots(roots, SAMPLE_BUDGET_S)
    # A copy: a thread that timed out may still be adding to its list.
    samples = {root: list(samplings[root].found) for root in roots}
    files = [f for picked in samples.values() for f in picked]
    results, nearby = _check(client, mappings, files) if files else ({}, {})
    # One server folder mapped to several disks: a disk without it is normal while another disk holds it.
    held = {(r.server_id, r.library_id, r.remote) for r in roots if samplings[r].opened}
    libraries = []
    for root in roots:
        picked = samples[root]
        sampling = samplings[root]
        if not picked and not sampling.opened and not sampling.timed_out:
            if (root.server_id, root.library_id, root.remote) in held:
                continue
        rows = []
        for f in picked:
            result = results.get(f["path"]) or {}
            rows.append(
                {
                    "path": f["path"],
                    "ok": bool(result.get("ok")),
                    "kind": result.get("kind") or ("" if result else "no_answer"),
                    "message": str(result.get("message") or ("" if result else "The node didn't answer for this file")),
                    "node_path": result.get("node_path") or map_app_path(f["path"], mappings),
                }
            )
        ok = bool(rows) and all(r["ok"] for r in rows)
        seen_nearby = _nearby_for(rows, nearby)
        error = None
        empty = False
        if not picked and sampling.timed_out:
            error = f"Timed out reading {sampling.folder or root.app_root} (no answer within {SAMPLE_BUDGET_S:g} s)"
        elif not picked and not sampling.opened:
            error = f"This app can't open {root.app_root}"
        elif not picked:
            empty = True
            error = f"No video files found near the top of {root.app_root}"
        libraries.append(
            {
                "server": root.server,
                "library": root.library,
                "app_root": root.app_root,
                "checked": len(rows),
                "ok": ok,
                "files": rows,
                "nearby": seen_nearby,
                "suggestion": None if ok or not rows else suggest_row(root.app_root, mappings, seen_nearby),
                "error": error,
                "empty": empty,
            }
        )
    return {"libraries": libraries}


def quick_access_check(client: NodeClient, mappings: list[PathMapping]) -> bool:
    """One file per mapping row: True when the node sees every one (and there was at least one to check).

    Reading this app's folders has the same budget as Test access, so a stalled mount fails the check instead of
    holding up the node's check.

    Args:
        client: The node.
        mappings: Its saved rows.

    Returns:
        Whether every sampled file was visible.
    """
    samplings = _sample_folders([row.app_prefix for row in mappings], SAMPLE_BUDGET_S, limit=1)
    if any(sampling.timed_out for sampling in samplings):
        logger.debug("Quick access check gave up: this app's own library folder didn't answer")
        return False
    files = [f for sampling in samplings for f in sampling.found]
    if not files:
        return False
    try:
        results, _ = _check(client, mappings, files)
    except Exception as exc:
        logger.debug("Quick access check failed: {}", exc)
        return False
    return all(bool((results.get(f["path"]) or {}).get("ok")) for f in files)


def default_mappings_for(servers: list[dict]) -> list[dict[str, str]]:
    """Pre-filled rows for Add node: one per distinct library folder, mapped to the same path on the node.

    A folder inside another listed folder is covered by that one's row, so it gets none of its own.

    Args:
        servers: The stored ``media_servers`` list.
    """
    roots = sorted({root.app_root for root in library_roots(servers)}, key=lambda p: (len(p), p))
    kept: list[str] = []
    for root in roots:
        if any(root == k or root.startswith(k.rstrip("/") + "/") for k in kept):
            continue
        kept.append(root)
    return [{"app_prefix": root, "node_prefix": root} for root in sorted(kept)]
