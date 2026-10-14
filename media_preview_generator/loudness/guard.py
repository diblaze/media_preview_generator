"""Positively identify Plex's database before loudness reads or writes, here or through its Plex database node.

A Plex database node (the Plex helper) imports this module for :class:`LoudnessPlexDb`, so everything at module
scope stays as light as ``markers/publishers``: the app's server and settings layers are imported where used.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from loguru import logger

from ..markers.publishers.base import Capability, CapabilityReport, DatabaseBusyError, PublishError
from ..markers.publishers.plex_db import (
    BUSY_TIMEOUT_S,
    PLEX_BUSY_PREFIX,
    TESTED_PMS_LABEL,
    WAIT_SLICE_S,
    LocalPlexDb,
    decode_extra_data,
    is_tested_pms_version,
    plex_busy_error,
    plex_db_path,
    publish_error_from_sqlite,
)
from .analyze import ANALYSIS_VERSION
from .plex_db import (
    check_schema,
    snapshot_from_json,
    snapshot_to_json,
    stream_from_json,
    stream_to_json,
)

if TYPE_CHECKING:
    from ..markers.publishers.plex_remote import RemotePlexDb
    from ..servers.base import ServerConfig
    from .plex_db import AudioStream, SourceFingerprint

# How long a read-only check trusts the Plex identity and database schema it just proved. Writes always prove them
# again. Checks run on several threads: each stamp is one assignment, so two threads meeting an expired stamp both
# prove it again, which is harmless.
TRUST_S = 60.0
# The Plex helper build that first runs loudness on Plex's machine; named in the "update the helper" message.
LOUDNESS_AGENT_VERSION = "1.2.0"


def plex_identity(server: Any) -> tuple[object, object]:
    """The connected Plex's ``(machineIdentifier, version)`` from its API.

    Raises:
        PublishError: UNREACHABLE when Plex didn't answer.
    """
    try:
        identity = server._connect().query("/identity")
        return identity.get("machineIdentifier"), identity.get("version")
    except Exception as exc:
        raise PublishError("Cannot verify the connected Plex server identity", state=Capability.UNREACHABLE) from exc


class LoudnessPlexDb(LocalPlexDb):
    """The shared SQLite writer, proven before each use to belong to the Plex ``identity()`` names.

    It runs in the app for a Plex on this machine, and on Plex's machine for a Plex database node, which is handed
    the identity the app just read from Plex's API: either way the database's own ``Preferences.xml`` must name the
    same server, and the server must be a tested Plex version.
    """

    def __init__(
        self,
        folder: str,
        *,
        label: str,
        identity: Callable[[], tuple[object, object]],
        mountinfo_path: str = "/proc/self/mountinfo",
        undo_problem: str = "",
    ) -> None:
        """Create the database.

        Args:
            folder: Plex's config folder as this process sees it.
            label: The server's name, for log lines.
            identity: Returns the connected Plex's ``(machineIdentifier, version)``; raises ``PublishError`` when it
                can't be read.
            mountinfo_path: For tests.
            undo_problem: Why the undo record wouldn't survive an update (a Plex database node's
                ``undo_record_problem``), or "": while set, the file checks are MISCONFIGURED, so nothing is written.
        """
        self.folder = Path(folder)
        self._undo_problem = undo_problem
        self.machine_identifier = ""
        self._read_identity = identity
        self._identity_proven_at: float | None = None
        self._schema_proven_at: float | None = None
        super().__init__(lambda: plex_db_path(folder), label=label, mountinfo_path=mountinfo_path)

    def _identity(self, *, trust_recent: bool = False) -> None:
        if (
            trust_recent
            and self._identity_proven_at is not None
            and time.monotonic() - self._identity_proven_at < TRUST_S
        ):
            return
        self._identity_proven_at = None
        machine, version = self._read_identity()
        try:
            actual = (
                ET.parse(self.folder / "Preferences.xml", forbid_dtd=True, forbid_entities=True, forbid_external=True)
                .getroot()
                .get("ProcessedMachineIdentifier")
            )
        except (OSError, ET.ParseError, DefusedXmlException):
            actual = None
        if not isinstance(machine, str) or not machine or not actual or actual != machine:
            raise PublishError(
                "Cannot prove this database belongs to the connected Plex server", state=Capability.MISCONFIGURED
            )
        if not is_tested_pms_version(version):
            raise PublishError(
                f"Loudness writes have only been verified with Plex {TESTED_PMS_LABEL}",
                state=Capability.UNSUPPORTED_SCHEMA,
            )
        self.machine_identifier = machine
        self._identity_proven_at = time.monotonic()

    def check_schema_for_read(self, conn: sqlite3.Connection) -> None:
        """``check_schema`` for a read, skipped when it passed in the last ``TRUST_S``."""
        if self._schema_proven_at is not None and time.monotonic() - self._schema_proven_at < TRUST_S:
            return
        self._schema_proven_at = None
        check_schema(conn)
        self._schema_proven_at = time.monotonic()

    @contextmanager
    def _database(self, *, read_only: bool, deadline: float) -> Iterator[sqlite3.Connection]:
        self._identity(trust_recent=read_only)
        if not read_only:
            report = self.file_checks(deadline=deadline)
            if not report.ready:
                raise PublishError(report.message, state=report.state)
        with super()._database(read_only=read_only, deadline=deadline) as conn:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                raise PublishError("Loudness writes require Plex's live WAL database", state=Capability.NEEDS_LOCAL_DB)
            yield conn

    def file_checks(self, *, deadline: float) -> CapabilityReport:
        """Retain the shared readiness states with loudness-specific wording; MISCONFIGURED while the undo record
        has nowhere lasting to go."""
        report = super().file_checks(deadline=deadline)
        if report.ready and self._undo_problem:
            return CapabilityReport(Capability.MISCONFIGURED, self._undo_problem)
        message = report.message.replace("markers", "loudness metadata").replace("Markers", "Loudness metadata")
        return CapabilityReport(report.state, message, report.details)

    def capability(self, *, deadline: float) -> CapabilityReport:
        """The file checks, then the identity and the loudness tables: ready to read and write, or why not."""
        try:
            report = self.file_checks(deadline=deadline)
            if not report.ready:
                return report
            with self._database(read_only=True, deadline=deadline) as conn:
                check_schema(conn)
        except PublishError as exc:
            return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc))
        except sqlite3.Error as exc:
            error = publish_error_from_sqlite(exc)
            return CapabilityReport(error.state or Capability.UNREACHABLE, str(error))
        return CapabilityReport(Capability.READY, "Plex loudness analysis is ready")


def _verify_streams(server: Any, streams: list[AudioStream]) -> None:
    by_item: dict[int, list[AudioStream]] = {}
    for stream in streams:
        by_item.setdefault(stream.metadata_item_id, []).append(stream)
    for item_id, expected in by_item.items():
        try:
            metadata = server._connect().query(f"/library/metadata/{item_id}")
            for stream in expected:
                parts = [
                    part
                    for part in metadata.findall(".//Part")
                    if part.get("id") == str(stream.part_id) and part.get("file") == stream.file
                ]
                matches = [
                    audio
                    for part in parts
                    for audio in part.findall("Stream")
                    if audio.get("id") == str(stream.id)
                    and audio.get("index") == str(stream.index)
                    and audio.get("streamType") == "2"
                ]
                if len(matches) != 1:
                    raise ValueError("Plex has not exposed the expected audio stream")
                audio = matches[0]
                fields = decode_extra_data(stream.extra_data)[0]
                if (
                    audio.get("canNormalizeLoudness") != "1"
                    or audio.get("loudnessAnalysisVersion") != ANALYSIS_VERSION
                    or fields.get("ln:loudnessAnalysisVersion") != ANALYSIS_VERSION
                ):
                    raise ValueError("Plex has not enabled the expected normalization")
                for key in ("loudness", "peak", "lra", "threshold", "gainOffset"):
                    if float(audio.get(key)) != float(fields[f"ln:{key}"]):
                        raise ValueError("Plex has not exposed the expected measurement")
        except Exception as exc:
            raise PublishError(
                "Loudness analysis awaits verification through Plex's API", state=Capability.UNREACHABLE
            ) from exc


class GuardedLoudnessDb(LoudnessPlexDb):
    """The shared SQLite writer with loudness-specific identity and version gates, for a Plex on this machine."""

    def __init__(self, cfg: ServerConfig, server: Any) -> None:
        from ..servers.base import ServerType

        if cfg.type is not ServerType.PLEX:
            raise PublishError("Loudness analysis requires a Plex server", state=Capability.MISCONFIGURED)
        folder = str((cfg.output or {}).get("plex_config_folder") or "").strip()
        if not folder:
            raise PublishError("Set the local Plex config folder for loudness analysis", state=Capability.MISCONFIGURED)
        self.server = server
        super().__init__(folder, label=cfg.name, identity=lambda: plex_identity(server))

    def verify_streams(self, streams: list[AudioStream]) -> None:
        """Require Plex's API to serve the exact indexed streams and normalization values.

        Args:
            streams: Fresh database rows with complete loudness metadata.

        Raises:
            PublishError: Plex cannot yet serve the matching analysis; retry later.
        """
        self._identity(trust_recent=True)
        _verify_streams(self.server, streams)


def _flag(result: dict, key: str) -> bool:
    value = result.get(key)
    if not isinstance(value, bool):
        raise PublishError(
            "The Plex database node sent a loudness answer this app can't read", state=Capability.AGENT_UNAVAILABLE
        )
    return value


class RemoteLoudnessDb:
    """Plex's database through its Plex database node (the Plex helper), for a Plex on another machine.

    The node runs the very functions a local database runs (``plex_db.read_streams`` … ``mark_item``) on a
    :class:`LoudnessPlexDb` there, proving its database against the identity this app reads from Plex's API on every
    request. What needs this machine stays here: Plex's API (its identity and the playback check) and the source
    file, whose fingerprint is checked again just before each write is sent.

    The node keeps the undo record (``loudness-writes.jsonl`` in its own ``CONFIG_DIR``), because the undo tool has
    to run beside the database file with Plex stopped.

    A write can't be cancelled or see the source file while the node waits for Plex's lock, so it is sent with a
    deadline of one ``WAIT_SLICE_S`` at a time: before each attempt the cancel and the source file are checked here,
    and a busy answer is tried again until the write's own deadline, as the in-process writer checks between its
    lock-wait slices.
    """

    def __init__(self, server: Any, remote: RemotePlexDb) -> None:
        """Create the transport.

        Args:
            server: The connected Plex.
            remote: That server's Plex database node, as ``plex_database`` resolves it.
        """
        self.server = server
        self.machine_identifier = ""
        self._remote = remote
        self._identity_read_at: float | None = None
        self._read: tuple[object, object] = (None, None)

    def _identity(self, *, trust_recent: bool = False) -> tuple[object, object]:
        if trust_recent and self._identity_read_at is not None and time.monotonic() - self._identity_read_at < TRUST_S:
            return self._read
        self._identity_read_at = None
        self._read = plex_identity(self.server)
        self._identity_read_at = time.monotonic()
        return self._read

    def _call(self, operation: str, body: dict, *, deadline: float, trust_recent: bool = True) -> dict:
        machine, version = self._identity(trust_recent=trust_recent)
        result = self._remote.loudness(
            operation,
            {"machine_identifier": machine, "pms_version": version, **body},
            deadline=deadline,
            needs=LOUDNESS_AGENT_VERSION,
        )
        if isinstance(machine, str):
            self.machine_identifier = machine
        return result

    def _report(self, operation: str, body: dict, *, deadline: float, identity: bool) -> CapabilityReport:
        from ..markers.publishers.plex_remote import report_from_json

        try:
            if identity:
                result = self._call(operation, body, deadline=deadline)
            else:
                result = self._remote.loudness(operation, body, deadline=deadline, needs=LOUDNESS_AGENT_VERSION)
            report = report_from_json(result.get("report"))
        except PublishError as exc:
            report = CapabilityReport(exc.state or Capability.AGENT_UNAVAILABLE, str(exc))
        except ValueError as exc:
            logger.debug("Plex helper: unreadable loudness report: {}", exc)
            report = CapabilityReport(
                Capability.AGENT_UNAVAILABLE, "The Plex database node sent a loudness answer this app can't read"
            )
        return CapabilityReport(report.state, report.message, {**report.details, "agent": self._remote.agent_details()})

    def file_checks(self, *, deadline: float) -> CapabilityReport:
        """The database file's checks, run on the node (found, local disk there, writable, held open by Plex)."""
        return self._report("file_checks", {}, deadline=deadline, identity=False)

    def capability(self, *, deadline: float) -> CapabilityReport:
        """The node's full loudness readiness: file checks, identity and tables."""
        return self._report("check", {}, deadline=deadline, identity=True)

    def read_streams(self, plex_paths: list[str], *, deadline: float) -> tuple[list[AudioStream], bool]:
        """``plex_db.read_streams``, run on the node."""
        result = self._call("streams", {"plex_paths": list(plex_paths)}, deadline=deadline)
        try:
            raw = result["streams"]
            if not isinstance(raw, list):
                raise ValueError("streams must be a list")
            return [stream_from_json(stream) for stream in raw], _flag(result, "in_plex")
        except (KeyError, ValueError) as exc:
            raise PublishError(
                "The Plex database node sent a loudness answer this app can't read", state=Capability.AGENT_UNAVAILABLE
            ) from exc

    def read_item_snapshot(self, metadata_item_id: int, *, deadline: float) -> tuple:
        """``plex_db.read_item_snapshot``, run on the node."""
        result = self._call("snapshot", {"metadata_item_id": metadata_item_id}, deadline=deadline)
        try:
            return snapshot_from_json(result.get("snapshot"))
        except ValueError as exc:
            raise PublishError(
                "The Plex database node sent a loudness answer this app can't read", state=Capability.AGENT_UNAVAILABLE
            ) from exc

    def write_stream(
        self,
        stream_id: int,
        fields: dict[str, str],
        *,
        deadline: float,
        expected: AudioStream | None = None,
        source: SourceFingerprint | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> bool:
        """``plex_db.write_stream``, run on the node; the source file and the cancel are checked here before each
        attempt."""
        body = {
            "stream_id": stream_id,
            "fields": dict(fields),
            "expected": stream_to_json(expected) if expected is not None else None,
        }
        result = self._write_in_slices(
            "write",
            body,
            deadline=deadline,
            source=source,
            cancel_check=cancel_check,
            cancelled="Loudness analysis cancelled before publication",
        )
        return _flag(result, "written")

    def mark_item(
        self,
        metadata_item_id: int,
        *,
        deadline: float,
        expected: tuple | None = None,
        source: SourceFingerprint | None = None,
        cancel_check: Callable[[], bool] | None = None,
        skipped: frozenset[tuple[str, int, str]] = frozenset(),
    ) -> bool:
        """``plex_db.mark_item``, run on the node; the source file and the cancel are checked here before each
        attempt."""
        body = {
            "metadata_item_id": metadata_item_id,
            "expected": snapshot_to_json(expected) if expected is not None else None,
            "skipped": sorted([str(h), int(i), str(c)] for h, i, c in skipped),
        }
        result = self._write_in_slices(
            "mark",
            body,
            deadline=deadline,
            source=source,
            cancel_check=cancel_check,
            cancelled="Loudness analysis cancelled before marking the item",
        )
        return _flag(result, "marked")

    def _write_in_slices(
        self,
        operation: str,
        body: dict,
        *,
        deadline: float,
        source: SourceFingerprint | None,
        cancel_check: Callable[[], bool] | None,
        cancelled: str,
    ) -> dict:
        """Send one write with a deadline of at most ``WAIT_SLICE_S``, again while the database is busy.

        Args:
            operation: ``write`` or ``mark``.
            body: The operation's fields.
            deadline: When the write as a whole stops waiting for the database.
            source: Checked before each attempt; a changed file stops the write.
            cancel_check: Checked before each attempt; a cancel stops the write.
            cancelled: The message of a cancelled write.

        Returns:
            The answer's ``result``.

        Raises:
            PublishError: Cancelled, the source changed, or the node refused; ``DatabaseBusyError`` once the
                database stayed busy past ``deadline``.
        """
        started = time.monotonic()
        trust_recent = False
        while True:
            if cancel_check and cancel_check():
                raise PublishError(cancelled)
            if source is not None:
                source.verify()
            attempt_deadline = min(deadline, time.monotonic() + WAIT_SLICE_S)
            try:
                # The identity is proven on the first attempt; a retry a second later may reuse that proof.
                return self._call(operation, body, deadline=attempt_deadline, trust_recent=trust_recent)
            except DatabaseBusyError as exc:
                if time.monotonic() >= deadline:
                    if str(exc).startswith(PLEX_BUSY_PREFIX):
                        raise plex_busy_error(time.monotonic() - started) from exc
                    raise
            trust_recent = True

    def verify_streams(self, streams: list[AudioStream]) -> None:
        """Require Plex's API to serve the streams just written (see :meth:`GuardedLoudnessDb.verify_streams`)."""
        self._identity(trust_recent=True)
        _verify_streams(self.server, streams)


def create_loudness_db(cfg: ServerConfig, server: Any) -> GuardedLoudnessDb | RemoteLoudnessDb:
    """The server's loudness database, independently of Intro & Credits consent: through its Plex database node
    when one is set up, else the guarded local file."""
    from ..markers.publishers.plex_db import PlexMarkerPublisher
    from ..markers.publishers.plex_remote import NO_NODE_REFUSAL, plex_database
    from ..markers.settings import load_server
    from ..servers.base import ServerType

    if cfg.type is not ServerType.PLEX:
        raise PublishError("Loudness analysis requires a Plex server", state=Capability.MISCONFIGURED)
    settings = load_server(cfg.markers, "plex")
    if settings.agent_enabled:
        db = plex_database(
            settings,
            path_provider=lambda: PlexMarkerPublisher.db_path_for(cfg),
            label=cfg.name or "",
        )
        return RemoteLoudnessDb(server, db)
    plex = (cfg.markers or {}).get("plex") or {}
    agent = plex.get("agent") if isinstance(plex, dict) else None
    if isinstance(agent, dict) and agent.get("enabled"):
        # Switched on in a block that doesn't load (no node chosen): never fall back to a local folder the user
        # meant not to use.
        raise PublishError(NO_NODE_REFUSAL.message, state=Capability.MISCONFIGURED)
    return GuardedLoudnessDb(cfg, server)


def loudness_capability(server: Any, cfg: ServerConfig) -> CapabilityReport:
    """Check the loudness backend without changing Plex or requiring marker consent."""
    try:
        db = create_loudness_db(cfg, server)
    except PublishError as exc:
        return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc))
    return db.capability(deadline=time.monotonic() + BUSY_TIMEOUT_S)
