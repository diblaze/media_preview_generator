"""Per-output ``.meta`` sidecar journal for source-aware skip-if-exists.

Background — what problem this solves:

The frame cache (process-wide, 10-min TTL) coalesces *concurrent* webhooks
for the same file. But when webhooks for the same file arrive *minutes
apart* (Sonarr fires immediately on import; Plex's own webhook fires
after its periodic library scan, often 30+ minutes later), the second
webhook arrives after the cache has expired. Today we re-extract frames
even though all publishers' outputs already exist on disk — wasted work.

Worse, if a user *replaces* the source file (Sonarr "upgrade" pulls a
higher-quality copy), the existing outputs are now stale. Plain
``output_paths.exists()`` skip-if-exists would happily reuse the old
BIF for a different source.

The ``.meta`` JSON sidecar records each source file's identity, size and
nanosecond modification/change times; freshness compares only the size and
nanosecond modification time (``same_recorded_source``). Device and inode
numbers change on every FUSE remount and every move between disks, so they
would rebuild unchanged files (#414). Older journals retain their
mtime-in-seconds/size checks for existing-output compatibility.
Cross-publisher reuse requires a recorded fingerprint so old frames
cannot seed a newly calculated Plex bundle after a source replacement.

Plex bundles are content-addressed (the path is a hash of the file's
content), so they carry no ``.meta``; only Emby and Jellyfin outputs do.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from .plex_hash import SourceFingerprint, get_source_fingerprint, same_recorded_source

#: Bumped whenever the on-disk schema changes. A sidecar in another schema
#: proves nothing either way, so its output is judged like one with no
#: sidecar (fresh); the next publish rewrites it in the current schema.
JOURNAL_SCHEMA_VERSION = 1


def _meta_path_for(output: Path) -> Path:
    """Return the ``.meta`` sidecar path for an output file.

    We append (not replace) the suffix so multiple outputs in the same
    directory don't collide and users can ``ls`` them next to the real
    files.
    """
    return output.with_suffix(output.suffix + ".meta")


def outputs_have_content(output_paths: Sequence[Path]) -> bool:
    """Whether there is at least one output and every one is on disk with data in it.

    A 0-byte file is what a power loss leaves when the name reached the disk
    before the data did; it holds no preview, so it counts as missing.

    Args:
        output_paths: Output files to check.

    Returns:
        True when ``output_paths`` is non-empty and every file is non-empty.
    """
    if not output_paths:
        return False
    try:
        return all(output.stat().st_size > 0 for output in output_paths)
    except OSError:
        return False


def _read_meta(meta_path: Path) -> dict | None:
    """Read a current-schema journal, treating missing or malformed data as unknown."""
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if int(data.get("schema", 0)) != JOURNAL_SCHEMA_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    return data


def _write_meta_document(meta_path: Path, payload: dict) -> None:
    """Replace a journal atomically."""
    tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                logger.debug("fsync failed for {}: {}", tmp_path, exc)
        os.replace(tmp_path, meta_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def write_meta(
    output_paths: Sequence[Path],
    canonical_path: str,
    *,
    publisher: str | None = None,
    source_fingerprint: SourceFingerprint | None = None,
) -> None:
    """Stamp every output with the source file's freshness fingerprint.

    Called by ``_publish_one`` immediately after a successful publish.
    Writes one ``.meta`` per output so any single output that survives
    (e.g. user manually deleted the others) still carries the
    fingerprint and the dispatcher can make a correct freshness call.

    Failures here never bubble — at worst we miss a future short-circuit
    and re-run FFmpeg.
    """
    try:
        fingerprint = source_fingerprint or get_source_fingerprint(canonical_path)
    except OSError:
        # Source vanished between publish and meta-write — leave the
        # outputs un-stamped so a future webhook re-publishes if the
        # source comes back.
        return

    source_mtime = fingerprint[3] // 1_000_000_000
    source_size = fingerprint[2]
    payload = {
        "schema": JOURNAL_SCHEMA_VERSION,
        "source_path": canonical_path,
        "source_mtime": source_mtime,
        "source_size": source_size,
        "source_fingerprint": list(fingerprint),
        "publisher": publisher or "",
        # Older app versions read ``sources``; keep it so a downgrade still understands the note.
        "sources": [
            {
                "path": canonical_path,
                "mtime": source_mtime,
                "size": source_size,
                "source_fingerprint": list(fingerprint),
            }
        ],
    }
    for output in output_paths:
        try:
            _write_meta_document(_meta_path_for(output), payload)
        except OSError as exc:
            # Don't let a write failure here mask a successful publish.
            logger.debug("Could not write journal meta for {}: {}", output, exc)


def outputs_fresh_for_source(
    output_paths: list[Path], canonical_path: str, *, require_source_fingerprint: bool = False
) -> bool:
    """Return True iff outputs exist and, where journals exist, the source matches.

    "Fresh" semantics:

    * Every entry in ``output_paths`` must exist on disk and hold data; a
      0-byte output counts as missing whatever its ``.meta`` says.
    * If **no** ``.meta`` sidecars exist at all (legacy outputs from
      before the journal feature shipped), assume fresh — preserves the
      pre-journal skip-if-exists behavior so upgrading the tool doesn't
      force a regeneration storm. The next successful publish stamps a
      ``.meta`` so subsequent calls go through the strict path.
    * If **any** ``.meta`` sidecar exists, at least one must record
      a fingerprint matching the current source. Conversely, if no
      recorded fingerprint matches, fresh is False — the source has been
      replaced (Sonarr quality upgrade, manual swap), so skip-if-exists
      should not fire.

    Schema mismatch and I/O errors on a particular ``.meta`` are
    ignored; they neither prove nor disprove freshness. The ``stat``
    of the source itself failing returns False — better to regenerate
    than gamble.

    New records compare size and nanosecond mtime. Legacy records retain
    mtime/size matching unless ``require_source_fingerprint`` is set; use
    that stricter check before transferring frames between publishers.
    """
    if not outputs_have_content(output_paths):
        return False

    try:
        fingerprint = get_source_fingerprint(canonical_path)
    except OSError:
        return False
    src_mtime = fingerprint[3] // 1_000_000_000
    src_size = fingerprint[2]

    saw_match = False
    saw_mismatch = False
    for output in output_paths:
        data = _read_meta(_meta_path_for(output))
        if data is None:
            continue
        if "source_fingerprint" in data:
            matches = same_recorded_source(data["source_fingerprint"], fingerprint)
        else:
            matches = not require_source_fingerprint and _legacy_stamp(data) == (src_mtime, src_size)
        if matches:
            saw_match = True
        else:
            saw_mismatch = True

    if saw_match:
        return True
    if saw_mismatch:
        return False
    # No usable meta on any output — legacy outputs from before the
    # journal feature; preserve the pre-journal skip-if-exists semantic
    # so upgrades don't force a regeneration storm.
    return not require_source_fingerprint


def _legacy_stamp(data: dict) -> tuple[int, int] | None:
    """Return a journal's recorded ``(mtime, size)``, or ``None`` when unusable."""
    try:
        return int(data.get("source_mtime", -1)), int(data.get("source_size", -1))
    except (TypeError, ValueError):
        return None


def clear_meta(output_paths: Sequence[Path]) -> None:
    """Remove ``.meta`` sidecars for the given outputs.

    Used by force-regenerate flows so a stale fingerprint can't shortcut
    a freshly-requested run, and to drop the leftover note beside a Plex
    bundle written by an older version. Best-effort.
    """
    for output in output_paths:
        try:
            _meta_path_for(output).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not remove stale meta for {}: {}", output, exc)
