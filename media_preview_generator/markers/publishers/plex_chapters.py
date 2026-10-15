"""Register complete external chapter images using Plex's existing chapter rows.

The local writer and Plex-side agent use the same guarded transaction. No chapters
are created: chapterless and unindexed items stay distinct. Plex keeps one chapter list
per title, so with several versions one owner version supplies its thumbnails; a version
split into several files is unsupported.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import Any

from PIL import Image

from ..settings import load_server
from .base import Capability, CapabilityReport, PublishError
from .plex_db import (
    TESTED_PMS_LABEL,
    TESTED_PMS_VERSIONS,
    LocalPlexDb,
    beside_another_plex,
    is_tested_pms_version,
    node_wording,
    plex_db_path,
    plex_machine_identifier,
    publish_error_from_sqlite,
)
from .plex_remote import (
    CHAPTERS_WAIT_FOR_THE_NODE,
    AgentClient,
    agent_older_than,
    node_refusal,
    report_from_json,
    report_wrong_node,
    saved_node_address,
    saved_node_uid,
    saved_server_node_id,
)

CHAPTER_CAPABILITY = "chapters_v1"
MAX_CHAPTERS = 1000
MAX_JPEG_BYTES = 16 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_THUMB_MEDIA_ID = re.compile(r"/library/media/(\d+)/chapterImages/")

# A library check asks the same two questions of every file: "which Plex is this?" and "is its database usable?".
# Read-only checks may reuse a recent answer; anything that writes asks again.
_RECENT_CHECK_TTL_S = 60.0
_recent_lock = threading.Lock()
_recent_identity: weakref.WeakKeyDictionary[Any, tuple[float, str, str]] = weakref.WeakKeyDictionary()
_recent_database_checks: dict[tuple[str, str, str], float] = {}


def _valid_bundle_hash(bundle_hash: str, source_size: int) -> bool:
    """Plex prefixes small-file hashes with their decimal size (output.plex_hash)."""
    if source_size < 65_536:
        prefix = str(source_size)
        return bundle_hash.startswith(prefix) and bool(_HASH.fullmatch(bundle_hash[len(prefix) :]))
    return bool(_HASH.fullmatch(bundle_hash))


class ChapterError(PublishError):
    """A chapter refusal with a stable retry classification."""

    def __init__(self, message: str, *, code: str = "registration", state: Capability | None = None) -> None:
        super().__init__(message, state=state)
        self.code = code


@dataclass(frozen=True)
class Chapter:
    """One existing Plex chapter, including the optimistic-concurrency identity."""

    index: int
    start_ms: int
    end_ms: int
    thumb_url: str
    row_id: int
    tag_id: int


@dataclass(frozen=True)
class ChapterTarget:
    """Exact indexed source and chapter rows read before generating any images."""

    rating_key: int
    media_id: int
    part_id: int
    bundle_hash: str
    source_path: str
    source_size: int
    source_updated_at: int | None
    chapters: tuple[Chapter, ...]
    machine_identifier: str
    # Plex has analysed this part (duration and audio/video streams recorded); chapters are written at that step, so
    # only then does an empty chapter list mean "no chapters". A backend that can't tell leaves it False. Derived, so
    # not part of the snapshot's identity: an app older than this field sends none, and register's whole-snapshot
    # comparison must not read that as Plex having changed the source.
    analysed: bool = field(default=False, compare=False)


def _target_changes(before: ChapterTarget, after: ChapterTarget) -> tuple[str, ...]:
    """Describe optimistic-concurrency changes without exposing source values."""
    changed = [
        field.name
        for field in fields(ChapterTarget)
        if field.name not in {"chapters", "analysed"} and getattr(before, field.name) != getattr(after, field.name)
    ]
    if len(before.chapters) != len(after.chapters):
        changed.append("chapters.count")
    else:
        changed.extend(
            f"chapters.{field.name}"
            for field in fields(Chapter)
            if any(
                getattr(left, field.name) != getattr(right, field.name)
                for left, right in zip(before.chapters, after.chapters, strict=True)
            )
        )
    return tuple(changed)


def target_to_json(target: ChapterTarget) -> dict:
    """Serialize a snapshot for the fixed agent protocol."""
    return asdict(target)


def _integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Expected a nonnegative whole number")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def target_from_json(raw: Any) -> ChapterTarget:
    """Reject malformed wire snapshots before opening the database."""
    if not isinstance(raw, dict) or not isinstance(raw.get("chapters"), list | tuple):
        raise ValueError("Expected a chapter snapshot")
    if len(raw["chapters"]) > MAX_CHAPTERS:
        raise ValueError("Too many chapters")
    chapters = tuple(
        Chapter(
            _integer(c["index"], minimum=1),
            _integer(c["start_ms"]),
            _integer(c["end_ms"]),
            _text(c["thumb_url"]),
            _integer(c["row_id"], minimum=1),
            _integer(c["tag_id"], minimum=1),
        )
        for c in raw["chapters"]
    )
    target = ChapterTarget(
        _integer(raw["rating_key"], minimum=1),
        _integer(raw["media_id"], minimum=1),
        _integer(raw["part_id"], minimum=1),
        _text(raw["bundle_hash"]),
        _text(raw["source_path"]),
        _integer(raw["source_size"]),
        None if raw["source_updated_at"] is None else _integer(raw["source_updated_at"]),
        chapters,
        _text(raw["machine_identifier"]),
        raw.get("analysed") is True,
    )
    if (
        not _valid_bundle_hash(target.bundle_hash, target.source_size)
        or not target.machine_identifier
        or not target.source_path
    ):
        raise ValueError("Invalid source identity")
    if [c.index for c in chapters] != list(range(1, len(chapters) + 1)):
        raise ValueError("Chapter indexes must be consecutive and start at one")
    for chapter in chapters:
        if chapter.end_ms < chapter.start_ms:
            raise ValueError(
                f"Invalid chapter timing for chapter {chapter.index}: "
                f"start {chapter.start_ms}ms, end {chapter.end_ms}ms"
            )
    for left, right in pairwise(chapters):
        # Images use chapter starts; Plex can round very short chapters to
        # zero duration or retain overlapping ends. Never rewrite that timing.
        if left.start_ms > right.start_ms:
            raise ValueError(
                f"Chapter start times are out of order: chapter {left.index} starts at {left.start_ms}ms "
                f"but chapter {right.index} starts at {right.start_ms}ms"
            )
    return target


def chapter_url(media_id: int, index: int, revision: str) -> str:
    """Native image route with a content revision for Plex's image cache."""
    return f"/library/media/{media_id}/chapterImages/{index}?mpgChapter={revision}"


def _own_chapter_starts(extra_data: Any) -> list[int] | None:
    """Read a part's own chapter starts (ms) from Plex's ``pv:chapters``, or None if unreadable."""
    try:
        chapter_list = json.loads(json.loads(extra_data)["pv:chapters"])["Chapters"].get("Chapter", [])
        if isinstance(chapter_list, dict):
            chapter_list = [chapter_list]
        return [round(float(chapter["start"]) * 1000) for chapter in chapter_list]
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _chapters_match(own_starts: list[int] | None, shared_rows: list[tuple]) -> bool:
    """True when a part's own chapters are the shared rows (starts within 1s, rounding aside)."""
    if not own_starts or len(own_starts) != len(shared_rows):
        return False
    return all(abs(own - row[1]) <= 1000 for own, row in zip(own_starts, shared_rows, strict=True))


class LocalChapters:
    """Chapter operations sharing the marker writer's filesystem and SQLite locks.

    Args:
        config_dir: Plex's config folder.
        mountinfo_path: For tests.
        on_node: It runs on a Plex database node, so a database file check's refusal is said about the node
            (``node_wording``), as markers and loudness say it, rather than about this app.
    """

    def __init__(self, config_dir: str, *, mountinfo_path: str = "/proc/self/mountinfo", on_node: bool = False) -> None:
        self.folder = Path(config_dir)
        self.database = LocalPlexDb(lambda: plex_db_path(config_dir), mountinfo_path=mountinfo_path)
        self._on_node = on_node

    def _identity(self, expected: str, version: str) -> None:
        actual = plex_machine_identifier(str(self.folder))
        if not expected or not actual or expected != actual:
            raise ChapterError(
                "Cannot prove this database belongs to the connected Plex server",
                code="unsupported",
                state=Capability.MISCONFIGURED,
            )
        if not is_tested_pms_version(version):
            raise ChapterError(
                f"Chapter registration has only been verified with Plex {TESTED_PMS_LABEL}",
                code="unsupported",
                state=Capability.UNSUPPORTED_SCHEMA,
            )

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        required = {
            "tags": {"id", "tag_type"},
            "taggings": {"id", "metadata_item_id", "tag_id", "index", "time_offset", "end_time_offset", "thumb_url"},
            "media_items": {"id", "metadata_item_id", "deleted_at", "proxy_type", "duration"},
            "media_streams": {"id", "media_part_id", "stream_type_id"},
            "media_parts": {"id", "media_item_id", "file", "hash", "size", "updated_at", "deleted_at", "extra_data"},
        }
        for table, columns in required.items():
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608 -- fixed names
            if columns - present:
                raise ChapterError(
                    "Plex chapter database schema differs from the tested schema",
                    code="unsupported",
                    state=Capability.UNSUPPORTED_SCHEMA,
                )
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='taggings'").fetchone():
            raise ChapterError(
                "Plex chapter table has unsupported triggers", code="unsupported", state=Capability.UNSUPPORTED_SCHEMA
            )
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise ChapterError(
                "Plex chapter writes require its live WAL database", code="unsupported", state=Capability.NEEDS_LOCAL_DB
            )

    def _guard(self, machine_identifier: str, version: str, deadline: float) -> None:
        # Plex keeps Preferences.xml readable by its own user only: a writer running as another user can't read the
        # identity either, and "can't write Plex's database" is the problem to name, not "can't prove which Plex".
        identity_readable = bool(plex_machine_identifier(str(self.folder)))
        if not identity_readable:
            self._file_checks(deadline)
        self._identity(machine_identifier, version)
        self._media_root()
        if identity_readable:
            self._file_checks(deadline)

    def _file_checks(self, deadline: float) -> None:
        report = self.database.file_checks(deadline=deadline)
        if not report.ready:
            # The refusal travels as its message alone, so a node words it before it is sent.
            report = node_wording(report) if self._on_node else report
            raise ChapterError(report.message, code="registration", state=report.state)

    def _media_root(self) -> Path:
        """Trust Plex's configured Media relocation, then confine images to that root."""
        media = self.folder / "Media"
        if media.is_symlink() and not media.is_dir():
            raise ChapterError(
                "Plex's Media link target is missing or inaccessible. Mount its target on the chapter writer.",
                state=Capability.MISCONFIGURED,
            )
        return media.resolve()

    def capability(self, machine_identifier: str, version: str, *, deadline: float) -> CapabilityReport:
        """Check readiness without requiring a chapter or marker tag to exist."""
        try:
            self._guard(machine_identifier, version, deadline)
            with self.database._database(read_only=True, deadline=deadline) as conn:
                self._schema(conn)
        except PublishError as exc:
            return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc))
        except sqlite3.Error as exc:
            error = publish_error_from_sqlite(exc)
            return CapabilityReport(error.state or Capability.UNREACHABLE, str(error))
        return CapabilityReport(
            Capability.READY,
            "Plex chapter registration is ready",
            {"capability": CHAPTER_CAPABILITY, "supported_pms_versions": [f"{v}.x" for v in TESTED_PMS_VERSIONS]},
        )

    @staticmethod
    def _read(
        conn: sqlite3.Connection, source_path: str, machine_identifier: str, item_id_hint: int | None
    ) -> ChapterTarget:
        rows = conn.execute(
            "SELECT mi.metadata_item_id, mi.id, mp.id, mp.hash, mp.file, mp.size, mp.updated_at, mp.extra_data, "
            "mi.duration > 0 AND EXISTS (SELECT 1 FROM media_streams ms WHERE ms.media_part_id=mp.id "
            "AND ms.stream_type_id IN (1, 2)) "
            "FROM media_parts mp JOIN media_items mi ON mi.id=mp.media_item_id "
            "WHERE mp.file=? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL "
            "AND (mi.proxy_type IS NULL OR mi.proxy_type=0)",
            (source_path,),
        ).fetchall()
        if item_id_hint is not None:
            rows = [row for row in rows if row[0] == item_id_hint]
        if not rows:
            raise ChapterError("Plex has not indexed this source yet", code="pending_index")
        if len(rows) != 1:
            raise ChapterError("Plex has more than one matching source", code="unsupported_source")
        row = rows[0]
        live_parts = conn.execute(
            "SELECT mi.id, mp.id FROM media_parts mp JOIN media_items mi ON mi.id=mp.media_item_id "
            "WHERE mi.metadata_item_id=? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL "
            "AND (mi.proxy_type IS NULL OR mi.proxy_type=0)",
            (row[0],),
        ).fetchall()
        if sum(1 for media_id, _ in live_parts if media_id == row[1]) != 1:
            raise ChapterError(
                "Chapter thumbnails do not support a version split into multiple files", code="unsupported_source"
            )
        chapters = conn.execute(
            "SELECT t.\"index\", t.time_offset, t.end_time_offset, COALESCE(t.thumb_url, ''), t.id, t.tag_id "
            "FROM taggings t JOIN tags ON tags.id=t.tag_id WHERE t.metadata_item_id=? AND tags.tag_type=9 "
            'ORDER BY t."index", t.id',
            (row[0],),
        ).fetchall()
        other_versions = {media_id for media_id, _ in live_parts if media_id != row[1]}
        if other_versions:
            if not chapters:
                raise ChapterError("No chapters", code="no_chapters")
            shown_from = {int(m.group(1)) for c in chapters if (m := _THUMB_MEDIA_ID.search(c[3]))}
            if shown_from & other_versions:
                raise ChapterError(
                    "Plex shows this title's chapter thumbnails from another version", code="other_version"
                )
            # Plex shows the thumbnails of the version whose own chapters it picked for the shared rows.
            if row[1] not in shown_from and not _chapters_match(_own_chapter_starts(row[7]), chapters):
                raise ChapterError(
                    "Plex's chapter list for this title comes from another version", code="other_version"
                )
        try:
            target = ChapterTarget(
                row[0],
                row[1],
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                tuple(Chapter(*c) for c in chapters),
                machine_identifier,
                bool(row[8]),
            )
            return target_from_json(target_to_json(target))
        except ValueError as exc:
            raise ChapterError(
                f"Plex source or chapter metadata is incomplete or unsupported: {exc}", code="unsupported"
            ) from exc
        except (KeyError, TypeError) as exc:
            raise ChapterError(
                "Plex source or chapter metadata is incomplete or unsupported", code="unsupported"
            ) from exc

    def read(
        self,
        source_path: str,
        machine_identifier: str,
        version: str,
        *,
        item_id_hint: int | None = None,
        deadline: float,
        reuse_recent_checks: bool = False,
    ) -> ChapterTarget:
        """Read one exact source under the common database lock.

        Args:
            reuse_recent_checks: Read side only (writes always prove identity again): skip the identity, file and
                schema checks when they all passed for this database within the last minute. The database is still
                opened and queried for every read, so the lock is never held between files.
        """
        key = (str(self.folder), machine_identifier, version)
        checked = reuse_recent_checks and _database_checked_recently(key)
        if not checked:
            self._guard(machine_identifier, version, deadline)
        try:
            with self.database._database(read_only=True, deadline=deadline) as conn:
                if not checked:
                    self._schema(conn)
                    _remember_database_checked(key)
                return self._read(conn, source_path, machine_identifier, item_id_hint)
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc

    @staticmethod
    def _within_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise ChapterError("Timed out validating chapter registration; no references changed", code="registration")

    def _image_path(self, target: ChapterTarget, index: int) -> Path:
        bundle = target.bundle_hash
        return (
            self.folder
            / "Media"
            / "localhost"
            / bundle[0]
            / f"{bundle[1:]}.bundle"
            / "Contents"
            / "Chapters"
            / f"chapter{index}.jpg"
        )

    def _images(
        self, target: ChapterTarget, revisions: dict[int, str], *, deadline: float
    ) -> dict[int, tuple[int, int]]:
        """Read, hash and decode each revised chapter image, outside Plex's write lock (decoding up to 1000 JPEGs is slow).

        Chapters that start after the video ends have no image, so ``revisions`` may be a non-empty subset.

        Returns:
            Each image's (size, mtime_ns) as it was read, for :meth:`_images_unchanged` inside the transaction.
        """
        if not revisions or not set(revisions) <= {chapter.index for chapter in target.chapters}:
            raise ChapterError("Registration requires images for existing chapters only", code="registration")
        root = self._media_root()
        seen: dict[int, tuple[int, int]] = {}
        for index, revision in revisions.items():
            self._within_deadline(deadline)
            if type(index) is not int or not isinstance(revision, str) or not _SHA256.fullmatch(revision):
                raise ChapterError("Invalid chapter image revision", code="registration")
            path = self._image_path(target, index)
            try:
                if not path.resolve().is_relative_to(root) or path.is_symlink():
                    raise ValueError("Image escapes the Plex Media folder")
                with path.open("rb") as stream:
                    data = stream.read(MAX_JPEG_BYTES + 1)
                if len(data) > MAX_JPEG_BYTES or hashlib.sha256(data).hexdigest() != revision:
                    raise ValueError("Image changed or exceeds the image limit")
                with Image.open(io.BytesIO(data)) as image:
                    if (
                        image.format != "JPEG"
                        or image.width > 8192
                        or image.height > 8192
                        or image.width * image.height > 16_000_000
                    ):
                        raise ValueError("Not a supported JPEG")
                    image.load()
                stat = path.stat()
                seen[index] = (stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError as exc:
                raise ChapterError(_image_not_found(index, path), code="registration") from exc
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise ChapterError(
                    f"Chapter {index} image is missing, invalid, or changed ({path})", code="registration"
                ) from exc
        return seen

    def _images_unchanged(self, target: ChapterTarget, seen: dict[int, tuple[int, int]]) -> None:
        """Inside the write transaction: every image is still the file :meth:`_images` validated."""
        for index, signature in seen.items():
            try:
                stat = self._image_path(target, index).stat()
            except OSError as exc:
                raise ChapterError(
                    f"Chapter {index} image is missing, invalid, or changed", code="registration"
                ) from exc
            if (stat.st_size, stat.st_mtime_ns) != signature:
                raise ChapterError(f"Chapter {index} image is missing, invalid, or changed", code="registration")

    def register(self, target: ChapterTarget, revisions: dict[int, str], version: str, *, deadline: float) -> None:
        """Compare the whole snapshot and update only the revised chapters' thumb_url, all or nothing."""
        target = target_from_json(target_to_json(target))
        self._guard(target.machine_identifier, version, deadline)
        seen = self._images(target, revisions, deadline=deadline)
        try:
            with self.database._database(read_only=False, deadline=deadline) as conn:
                self.database._begin_write(conn, deadline)
                try:
                    self._identity(target.machine_identifier, version)
                    self._schema(conn)
                    current = self._read(conn, target.source_path, target.machine_identifier, target.rating_key)
                    if current != target:
                        raise ChapterError(
                            "Plex source or chapters changed while images were generated "
                            f"(changed fields: {', '.join(_target_changes(target, current))})",
                            code="source_changed",
                        )
                    self._images_unchanged(target, seen)
                    own_prefix = f"/library/media/{target.media_id}/chapterImages/"
                    for chapter in target.chapters:
                        if chapter.index not in revisions:
                            # Drop only our own earlier registration; Plex-native thumbs stay.
                            if chapter.thumb_url.startswith(own_prefix) and "?mpgChapter=" in chapter.thumb_url:
                                conn.execute("UPDATE taggings SET thumb_url='' WHERE id=?", (chapter.row_id,))
                            continue
                        self._within_deadline(deadline)
                        desired = chapter_url(target.media_id, chapter.index, revisions[chapter.index])
                        if chapter.thumb_url != desired:
                            conn.execute("UPDATE taggings SET thumb_url=? WHERE id=?", (desired, chapter.row_id))
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc


def _image_not_found(index: int, path: Path) -> str:
    """The refusal of a chapter image that isn't where Plex reads it.

    Through a Plex database node the app writes the images into its own Plex config folder and the node looks for them
    in Plex's: a folder of another Plex (or a copy) on the app's side leaves every image missing here.
    """
    return (
        f"Chapter {index} image isn't at {path}, where this Plex reads it. The Plex config folder chapter images are "
        "written to must be this Plex's own folder."
    )


def _another_plexs_folder(config: Any, machine: str) -> str:
    """Why this app's Plex config folder for the server is another Plex's, or "" when it isn't (or can't tell).

    Through a Plex database node, this app still writes the chapter images (into that folder) and the node registers
    them in Plex's database: images written into another Plex's folder are never where this Plex reads them, so each
    file would fail its retries one by one.
    """
    folder = str((getattr(config, "output", None) or {}).get("plex_config_folder") or "").strip()
    theirs = plex_machine_identifier(folder) if folder else ""
    if not theirs or not machine or theirs == machine:
        return ""
    return (
        f"This server's Plex config folder ({folder}) belongs to a different Plex server, so chapter images written "
        "there never reach this one. Set it to this Plex's own config folder in Edit → Connection."
    )


def _database_checked_recently(key: tuple[str, str, str]) -> bool:
    with _recent_lock:
        checked_at = _recent_database_checks.get(key)
    return checked_at is not None and time.monotonic() - checked_at < _RECENT_CHECK_TTL_S


def _remember_database_checked(key: tuple[str, str, str]) -> None:
    with _recent_lock:
        _recent_database_checks[key] = time.monotonic()


def _server_identity(server: Any, plex: Any, *, reuse_recent: bool) -> tuple[str, str]:
    """The connected Plex's machine identifier and version; only successful answers are kept."""
    if reuse_recent:
        recent = None
        with _recent_lock, contextlib.suppress(TypeError):
            recent = _recent_identity.get(server)
        if recent is not None and time.monotonic() - recent[0] < _RECENT_CHECK_TTL_S:
            return recent[1], recent[2]
    identity = plex.query("/identity")
    machine = identity.get("machineIdentifier") or ""
    version = identity.get("version") or ""
    if machine and version:
        with _recent_lock, contextlib.suppress(TypeError):
            _recent_identity[server] = (time.monotonic(), machine, version)
    return machine, version


def _context(server: Any, server_config: Any = None, *, reuse_recent: bool = False) -> tuple[Any, str, str, Any]:
    try:
        plex = server._connect()
        machine, version = _server_identity(server, plex, reuse_recent=reuse_recent)
    except Exception as exc:
        with _recent_lock, contextlib.suppress(TypeError):
            _recent_identity.pop(server, None)  # a server that just failed is not one to trust from memory
        raise ChapterError(
            "Cannot verify Plex server identity for chapter registration",
            code="registration",
            state=Capability.UNREACHABLE,
        ) from exc
    config = server_config if server_config is not None else server._server_config
    settings = load_server(getattr(config, "markers", None), "plex")
    backend: AgentClient | LocalChapters
    if settings.agent_enabled:
        # The server's own config is the job's copy from when it started: compare its node with the saved one.
        live = partial(saved_server_node_id, str(getattr(config, "id", "") or "")) if server_config is None else None
        refusal = node_refusal(settings, live)
        if refusal is not None:
            raise ChapterError(refusal.message, state=refusal.state)
        wrong_folder = _another_plexs_folder(config, machine)
        if wrong_folder:
            raise ChapterError(wrong_folder, code="unsupported", state=Capability.MISCONFIGURED)
        backend = AgentClient(
            settings.agent_url,
            settings.agent_token,
            name=getattr(settings, "agent_node_name", ""),
            refusal=lambda: node_refusal(settings, live),
            node_uid=lambda: saved_node_uid(settings),
            wrong_node=lambda: report_wrong_node(settings),
            address=lambda: saved_node_address(settings),
        )
    else:
        folder = str((getattr(config, "output", None) or {}).get("plex_config_folder") or "")
        if not folder:
            raise ChapterError(
                "Set a local Plex config folder or choose a Plex database node in Edit → Connection",
                state=Capability.NEEDS_LOCAL_DB,
            )
        backend = LocalChapters(folder)
    return backend, machine, version, plex


# Helpers older than this refuse Plex 1.43.5 with a bare "only verified" message; say which update fixes it instead.
_AGENT_FOR_PMS_1_43_5 = "1.1.2"


def _server_name(server: Any, server_config: Any = None) -> str:
    config = server_config if server_config is not None else server._server_config
    return str(getattr(config, "name", "") or "")


def _running(client: AgentClient) -> str:
    return f"The Plex database node {client.describe()} is version {client.version or 'unknown'}"


def _remote(
    client: AgentClient, operation: str, machine: str, version: str, *, server_name: str = "", **body: Any
) -> dict:
    """One chapter operation on the Plex database node, after the checks that it can run it for this Plex.

    Args:
        client: The server's Plex database node.
        operation: ``check``, ``read`` or ``register``.
        machine: The connected Plex's machine identifier.
        version: The connected Plex's version.
        server_name: The server's name, for the refusal of a node beside a different Plex.
        **body: The operation's own fields.
    """
    identity = client.ping(timeout=5)
    if CHAPTER_CAPABILITY not in client.capabilities:
        raise ChapterError(
            f"{_running(client)}; chapter thumbnails need version 1.1.0 or newer. Update the node.",
            state=Capability.PLUGIN_OUTDATED,
        )
    if version.startswith("1.43.5.") and agent_older_than(client.version, _AGENT_FOR_PMS_1_43_5):
        raise ChapterError(
            f"{_running(client)}; chapter thumbnails for Plex 1.43.5 need version {_AGENT_FOR_PMS_1_43_5} or newer. "
            "Update the node.",
            state=Capability.PLUGIN_OUTDATED,
        )
    if not machine:
        raise ChapterError(
            f"Plex didn't say which server it is, so this app can't tell whether the Plex database node "
            f"{client.describe()} is beside it.",
            state=Capability.MISCONFIGURED,
        )
    # A node that can't say (it can't read Plex's Preferences.xml: usually it runs as another user than Plex) is asked
    # anyway: its own checks name what it can't read or write, and it refuses every operation until it can prove
    # which Plex it is beside.
    theirs = identity.get("machine_identifier")
    if theirs and theirs != machine:
        raise ChapterError(
            beside_another_plex(client.url, server_name, "chapter thumbnails", client.name),
            state=Capability.MISCONFIGURED,
        )
    result = client.post(
        f"/v1/chapters/{operation}",
        {
            "machine_identifier": machine,
            "pms_version": version,
            "deadline_s": 10,
            **body,
        },
        timeout=13,
        unreachable=CHAPTERS_WAIT_FOR_THE_NODE,
    )
    if result.get("capability") != CHAPTER_CAPABILITY:
        raise ChapterError(
            f"The Plex database node {client.describe()} sent no chapter answer. Update the node.",
            state=Capability.PLUGIN_OUTDATED,
        )
    return result


def chapter_capability(server: Any, server_config: Any = None) -> CapabilityReport:
    """Readiness independently of marker settings, consent, or Plex Pass."""
    backend: AgentClient | LocalChapters | None = None
    try:
        backend, machine, version, _ = _context(server, server_config)
        if isinstance(backend, AgentClient):
            checked = _remote(backend, "check", machine, version, server_name=_server_name(server, server_config))
            return report_from_json(checked["report"])
        return backend.capability(machine, version, deadline=time.monotonic() + 10)
    except PublishError as exc:
        return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc))
    except (KeyError, TypeError, ValueError):
        node = f" {backend.describe()}" if isinstance(backend, AgentClient) else ""
        return CapabilityReport(
            Capability.AGENT_UNAVAILABLE, f"The Plex database node{node} sent a chapter answer this app can't read"
        )


def resolve_chapter_target(
    server: Any, canonical_path: str, *, item_id_hint: int | None = None, reuse_recent_checks: bool = False
) -> ChapterTarget:
    """Resolve Plex's original source path to a guarded chapter snapshot.

    Args:
        server: The Plex server.
        canonical_path: Plex's original path of the source.
        item_id_hint: Plex's item id, when known.
        reuse_recent_checks: Read-side checks only: reuse a Plex identity and local database check made in the last
            minute instead of repeating them for every file. The Plex connection is still made every time, and
            writes (:func:`register_chapters`, :meth:`LocalChapters.register`) always prove identity again.
    """
    backend, machine, version, _ = _context(server, reuse_recent=reuse_recent_checks)
    if isinstance(backend, AgentClient):
        try:
            result = _remote(
                backend,
                "read",
                machine,
                version,
                server_name=_server_name(server),
                source_path=canonical_path,
                item_id_hint=item_id_hint,
            )
            target = target_from_json(result["target"])
            if target.machine_identifier != machine or target.source_path != canonical_path:
                raise ValueError("Wrong source identity")
            return target
        except (KeyError, TypeError, ValueError) as exc:
            raise ChapterError(
                f"The Plex database node {backend.describe()} sent a chapter snapshot this app can't read"
            ) from exc
    return backend.read(
        canonical_path,
        machine,
        version,
        item_id_hint=item_id_hint,
        deadline=time.monotonic() + 10,
        reuse_recent_checks=reuse_recent_checks,
    )


def register_chapters(
    server: Any,
    target: ChapterTarget,
    revisions: dict[int, str],
    *,
    verify_source: Callable[[], None] | None = None,
) -> None:
    """Register and verify images, allowing one guarded timestamp-only refresh.

    A source-fingerprint verifier is required to refresh an otherwise identical
    snapshot. Changed chapter references remain a conflict, even if another
    publisher generated them while this job was extracting its images.
    """
    backend, machine, version, _ = _context(server)
    if machine != target.machine_identifier:
        raise ChapterError("Connected Plex server changed during chapter generation", code="source_changed")

    def register(snapshot: ChapterTarget) -> None:
        if isinstance(backend, AgentClient):
            _remote(
                backend,
                "register",
                machine,
                version,
                server_name=_server_name(server),
                target=target_to_json(snapshot),
                revisions=[{"index": index, "sha256": sha} for index, sha in revisions.items()],
            )
        else:
            backend.register(snapshot, revisions, version, deadline=time.monotonic() + 10)

    if verify_source is not None:
        verify_source()
    try:
        register(target)
    except ChapterError as exc:
        if exc.code != "source_changed" or verify_source is None:
            raise
        fresh = resolve_chapter_target(server, target.source_path, item_id_hint=target.rating_key)
        verify_source()
        changed = _target_changes(target, fresh)
        if changed != ("source_updated_at",):
            raise ChapterError(
                "Plex source or chapters changed while images were generated "
                f"(changed fields: {', '.join(changed) or 'unconfirmed concurrent change'})",
                code="source_changed",
            ) from exc
        # The second write still compares its complete snapshot inside the
        # transaction. A further change fails normally; there is no retry loop.
        register(fresh)
        target = fresh
    if verify_source is not None:
        verify_source()
    verify_chapters(server, target, revisions)
    if verify_source is not None:
        verify_source()


def verify_chapters(server: Any, target: ChapterTarget, revisions: dict[int, str]) -> None:
    """Verify references even when an earlier commit succeeded but API read-back failed."""
    try:
        plex = server._connect()
        if plex.query("/identity").get("machineIdentifier") != target.machine_identifier:
            raise ValueError("Connected Plex server changed")
        result = plex.query(f"/library/metadata/{target.rating_key}?includeChapters=1")
        observed = {int(c.get("index")): c.get("thumb") for c in result.findall(".//Chapter")}
        expected = {index: chapter_url(target.media_id, index, revision) for index, revision in revisions.items()}
        if {index: observed.get(index) for index in expected} != expected:
            raise ValueError("Plex has not exposed the registered chapters")
    except Exception as exc:
        raise ChapterError("Chapter images await verification through Plex's API", code="registration") from exc
