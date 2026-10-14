"""Positively identify the local Plex database before loudness reads or writes."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from ..markers.publishers.base import Capability, CapabilityReport, PublishError
from ..markers.publishers.plex_db import (
    BUSY_TIMEOUT_S,
    TESTED_PMS_LABEL,
    LocalPlexDb,
    decode_extra_data,
    is_tested_pms_version,
    plex_db_path,
    publish_error_from_sqlite,
)
from ..servers.base import ServerConfig, ServerType
from .analyze import ANALYSIS_VERSION
from .plex_db import check_schema

if TYPE_CHECKING:
    from .plex_db import AudioStream

# How long a read-only check trusts the Plex identity and database schema it just proved. Writes always prove them
# again. Checks run on several threads: each stamp is one assignment, so two threads meeting an expired stamp both
# prove it again, which is harmless.
TRUST_S = 60.0


class GuardedLoudnessDb(LocalPlexDb):
    """The shared SQLite writer with loudness-specific identity and version gates."""

    def __init__(self, cfg: ServerConfig, server: Any) -> None:
        if cfg.type is not ServerType.PLEX:
            raise PublishError("Loudness analysis requires a Plex server", state=Capability.MISCONFIGURED)
        plex = (cfg.markers or {}).get("plex") or {}
        agent = plex.get("agent") if isinstance(plex, dict) else None
        if isinstance(agent, dict) and agent.get("enabled"):
            raise PublishError(
                "Loudness analysis requires a local Plex database; the Plex helper does not support loudness",
                state=Capability.MISCONFIGURED,
            )
        folder = str((cfg.output or {}).get("plex_config_folder") or "").strip()
        if not folder:
            raise PublishError("Set the local Plex config folder for loudness analysis", state=Capability.MISCONFIGURED)
        self.folder = Path(folder)
        self.server = server
        self.machine_identifier = ""
        self._identity_proven_at: float | None = None
        self._schema_proven_at: float | None = None
        super().__init__(lambda: plex_db_path(folder), label=cfg.name)

    def _identity(self, *, trust_recent: bool = False) -> None:
        if (
            trust_recent
            and self._identity_proven_at is not None
            and time.monotonic() - self._identity_proven_at < TRUST_S
        ):
            return
        self._identity_proven_at = None
        try:
            identity = self.server._connect().query("/identity")
            machine = identity.get("machineIdentifier")
            version = identity.get("version")
        except Exception as exc:
            raise PublishError(
                "Cannot verify the connected Plex server identity", state=Capability.UNREACHABLE
            ) from exc
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
        """Retain the shared readiness states with loudness-specific wording."""
        report = super().file_checks(deadline=deadline)
        message = report.message.replace("markers", "loudness metadata").replace("Markers", "Loudness metadata")
        return CapabilityReport(report.state, message, report.details)

    def verify_streams(self, streams: list[AudioStream]) -> None:
        """Require Plex's API to serve the exact indexed streams and normalization values.

        Args:
            streams: Fresh database rows with complete loudness metadata.

        Raises:
            PublishError: Plex cannot yet serve the matching analysis; retry later.
        """
        self._identity(trust_recent=True)
        by_item: dict[int, list[AudioStream]] = {}
        for stream in streams:
            by_item.setdefault(stream.metadata_item_id, []).append(stream)
        for item_id, expected in by_item.items():
            try:
                metadata = self.server._connect().query(f"/library/metadata/{item_id}")
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


def create_loudness_db(cfg: ServerConfig, server: Any) -> GuardedLoudnessDb:
    """Build the guarded local backend independently of Intro & Credits settings."""
    return GuardedLoudnessDb(cfg, server)


def loudness_capability(server: Any, cfg: ServerConfig) -> CapabilityReport:
    """Check the loudness backend without changing Plex or requiring marker consent."""
    try:
        db = create_loudness_db(cfg, server)
        deadline = time.monotonic() + BUSY_TIMEOUT_S
        report = db.file_checks(deadline=deadline)
        if not report.ready:
            return report
        with db._database(read_only=True, deadline=deadline) as conn:
            check_schema(conn)
    except PublishError as exc:
        return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc))
    except sqlite3.Error as exc:
        error = publish_error_from_sqlite(exc)
        return CapabilityReport(error.state or Capability.UNREACHABLE, str(error))
    return CapabilityReport(Capability.READY, "Plex loudness analysis is ready")
