"""Loudness through a Plex database node: the node runs the app's own loudness reads and writes on Plex's machine.

Both halves are real here: a Plex-shaped WAL database in a node's Plex folder, the role mounted on a bare Flask app,
and the app's ``RemoteLoudnessDb`` reaching it through ``AgentClient`` (in-process, via ``FlaskSession``). The app side
calls the very module functions the loudness job calls (``loudness.plex_db.read_streams`` … ``mark_item``), so the
delegation is what's under test, not a second code path.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import Element

import pytest

from media_preview_generator.loudness import plex_db as loudness_db
from media_preview_generator.loudness.guard import LoudnessPlexDb, RemoteLoudnessDb
from media_preview_generator.markers.publishers import plex_db, plex_remote
from media_preview_generator.markers.publishers.base import Capability, PublishError
from media_preview_generator.markers.publishers.plex_chapters import CHAPTER_CAPABILITY
from media_preview_generator.markers.publishers.plex_remote import RemotePlexDb
from media_preview_generator.nodes.server import plex_db as plex_db_role
from tests.loudness.test_plex_db import BEFORE, FIELDS, FILE, FIX, PLEX_ANALYSED, PLEX_ITEM_TRIGGERS
from tests.markers.test_plex_db_publisher import _mountinfo, linux_mount_table
from tests.nodes.plex_db_harness import NODE_URL, TOKEN, FlaskSession, node_app

MACHINE = "plex-a"
VERSION = "1.43.4.12345-abc123"
AUTH = {"Authorization": f"Bearer {TOKEN}", plex_remote.PROTOCOL_HEADER: str(plex_remote.AGENT_PROTOCOL)}


def _deadline() -> float:
    return time.monotonic() + 10


@pytest.fixture(autouse=True)
def plex_holds_the_database(monkeypatch):
    """Stand in for Plex having its database open through this very path (the role's lock proof)."""
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """The node's view of Plex's config folder: a 1.43 WAL database with one episode, and Plex's Preferences.xml."""
    folder = tmp_path / "node" / "Plex Media Server"
    path = Path(plex_db.plex_db_path(str(folder)))
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("BEGIN;\n" + (FIX / "plex_schema_1_43.sql").read_text() + PLEX_ITEM_TRIGGERS + "\nCOMMIT;")
    conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (1, 4, 'Ep')")
    conn.execute("INSERT INTO media_items (id, metadata_item_id, duration) VALUES (1, 1, 424000)")
    conn.execute("INSERT INTO media_parts (id, media_item_id, file) VALUES (1, 1, ?)", (FILE,))
    conn.executemany(
        'INSERT INTO media_streams (id, stream_type_id, "index", codec, extra_data, media_part_id, media_item_id) '
        "VALUES (?, ?, ?, ?, ?, 1, 1)",
        [(10, 1, 0, "h264", None), (11, 2, 1, "aac", BEFORE), (12, 2, 2, "aac", None)],
    )
    conn.commit()
    conn.close()
    (folder / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{MACHINE}"/>')
    # The node's own CONFIG_DIR: where it keeps the undo record.
    node_config = tmp_path / "node-config"
    node_config.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(node_config))
    return folder


@pytest.fixture
def node(folder, tmp_path):
    return node_app(str(folder), mountinfo_path=_mountinfo(tmp_path)).test_client()


def _plex(machine: str = MACHINE, version: str = VERSION) -> MagicMock:
    server = MagicMock()
    server._connect.return_value.query.return_value = Element(
        "MediaContainer", machineIdentifier=machine, version=version
    )
    return server


def _remote(node, server=None) -> tuple[RemoteLoudnessDb, FlaskSession]:
    session = FlaskSession(node)
    return RemoteLoudnessDb(server or _plex(), RemotePlexDb(NODE_URL, TOKEN, session=session)), session


def _extra(folder: Path, table: str, row_id: int) -> str | None:
    conn = sqlite3.connect(plex_db.plex_db_path(str(folder)))
    try:
        return conn.execute(f"SELECT extra_data FROM {table} WHERE id = ?", (row_id,)).fetchone()[0]  # noqa: S608
    finally:
        conn.close()


def _local(folder: Path, tmp_path: Path) -> LoudnessPlexDb:
    return LoudnessPlexDb(
        str(folder), label="local", identity=lambda: (MACHINE, VERSION), mountinfo_path=_mountinfo(tmp_path)
    )


class TestReadsAreTheLocalReads:
    def test_the_streams_are_exactly_what_a_local_read_returns(self, node, folder, tmp_path):
        db, _ = _remote(node)
        remote = loudness_db.read_streams(db, [FILE, "/other.mkv"], deadline=_deadline())
        assert remote == loudness_db.read_streams(_local(folder, tmp_path), [FILE, "/other.mkv"], deadline=_deadline())
        streams, in_plex = remote
        assert in_plex is True and [(s.id, s.codec) for s in streams] == [(11, "aac"), (12, "aac")]

    def test_a_file_plex_doesnt_have_is_not_in_plex(self, node):
        db, _ = _remote(node)
        assert loudness_db.read_streams(db, ["/other.mkv"], deadline=_deadline()) == ([], False)

    def test_the_snapshot_is_exactly_what_a_local_snapshot_returns(self, node, folder, tmp_path):
        db, _ = _remote(node)
        snapshot = loudness_db.read_item_snapshot(db, 1, deadline=_deadline())
        assert snapshot == loudness_db.read_item_snapshot(_local(folder, tmp_path), 1, deadline=_deadline())
        assert len(snapshot) == 2

    def test_the_readiness_check_is_ready_and_names_the_node(self, node):
        db, _ = _remote(node)
        report = db.capability(deadline=_deadline())
        assert report.state is Capability.READY
        assert report.details["agent"]["version"] == plex_db_role.AGENT_VERSION


class TestWritesAreTheLocalWrites:
    def test_a_write_is_byte_identical_to_plexs_own_and_the_undo_record_stays_on_the_node(self, node, folder, tmp_path):
        db, _ = _remote(node)
        streams, _ = loudness_db.read_streams(db, [FILE], deadline=_deadline())
        assert loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline(), expected=streams[0]) is True
        assert _extra(folder, "media_streams", 11) == PLEX_ANALYSED
        record, receipt = [
            json.loads(line) for line in (tmp_path / "node-config" / "loudness-writes.jsonl").read_text().splitlines()
        ]
        assert (record["stream_id"], record["before"], record["after"]) == (11, BEFORE, PLEX_ANALYSED)
        assert record["database"] == loudness_db.database_identity(plex_db.plex_db_path(str(folder)))
        assert receipt["transaction_id"] == record["transaction_id"]

    def test_a_second_write_changes_nothing(self, node, folder):
        db, session = _remote(node)
        loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline())
        other = {**FIELDS, "ln:loudness": "-99.00"}
        assert loudness_db.write_stream(db, 11, other, deadline=_deadline()) is False
        assert _extra(folder, "media_streams", 11) == PLEX_ANALYSED
        # The capability is asked for once; every answer after that says it is still there.
        assert session.paths == ["/v1/ping", "/v1/loudness/write", "/v1/loudness/write"]

    def test_a_stream_that_changed_after_the_snapshot_is_refused_as_a_changed_source(self, node, folder):
        db, _ = _remote(node)
        stream = loudness_db.read_streams(db, [FILE], deadline=_deadline())[0][0]
        with sqlite3.connect(plex_db.plex_db_path(str(folder))) as conn:
            conn.execute("UPDATE media_parts SET hash = 'replaced' WHERE id = 1")
        conn.close()
        # The type survives the trip: the job retries a changed source later instead of failing the file.
        with pytest.raises(loudness_db.SourceChangedError, match="source changed"):
            loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline(), expected=stream)
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_the_item_is_marked_once_every_stream_is_done(self, node, folder):
        db, _ = _remote(node)
        snapshot = loudness_db.read_item_snapshot(db, 1, deadline=_deadline())
        loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline())
        assert loudness_db.mark_item(db, 1, deadline=_deadline(), expected=snapshot) is False  # stream 12 to do
        loudness_db.write_stream(db, 12, FIELDS, deadline=_deadline())
        assert loudness_db.mark_item(db, 1, deadline=_deadline(), expected=snapshot) is True
        assert json.loads(_extra(folder, "metadata_items", 1))[loudness_db.VERSION_FIELD] == "0.02"

    def test_a_stream_known_undecodable_doesnt_hold_the_mark_back(self, node, folder):
        db, _ = _remote(node)
        with sqlite3.connect(plex_db.plex_db_path(str(folder))) as conn:
            conn.execute("UPDATE media_parts SET hash = 'h1' WHERE id = 1")
        conn.close()
        loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline())
        assert loudness_db.mark_item(db, 1, deadline=_deadline(), skipped=frozenset({("h1", 2, "aac")})) is True

    def test_a_version_added_after_the_snapshot_stops_the_mark(self, node, folder):
        db, _ = _remote(node)
        snapshot = loudness_db.read_item_snapshot(db, 1, deadline=_deadline())
        for stream_id in (11, 12):
            loudness_db.write_stream(db, stream_id, FIELDS, deadline=_deadline())
        with sqlite3.connect(plex_db.plex_db_path(str(folder))) as conn:
            conn.execute("INSERT INTO media_items (id, metadata_item_id, duration) VALUES (2, 1, 1)")
            conn.execute("INSERT INTO media_parts (id, media_item_id, file) VALUES (2, 2, '/data/kids/new.mkv')")
            conn.execute(
                'INSERT INTO media_streams (id, stream_type_id, "index", codec, media_part_id, media_item_id) '
                "VALUES (20, 2, 1, 'aac', 2, 2)"
            )
        conn.close()
        loudness_db.write_stream(db, 20, FIELDS, deadline=_deadline())
        with pytest.raises(loudness_db.SourceChangedError, match="versions changed"):
            loudness_db.mark_item(db, 1, deadline=_deadline(), expected=snapshot)
        assert _extra(folder, "metadata_items", 1) is None

    def test_a_source_file_that_changed_here_is_never_sent(self, node, folder, tmp_path):
        db, session = _remote(node)
        source = tmp_path / "local.mkv"
        source.write_bytes(b"old")
        fingerprint = loudness_db.SourceFingerprint.read(str(source))
        # A different size: Windows may not move the modified time for a rewrite within the same clock tick.
        source.write_bytes(b"replaced")
        with pytest.raises(loudness_db.SourceChangedError):
            loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline(), source=fingerprint)
        assert "/v1/loudness/write" not in session.paths
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_a_cancelled_job_sends_nothing(self, node, folder):
        db, session = _remote(node)
        with pytest.raises(PublishError, match="cancelled"):
            loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline(), cancel_check=lambda: True)
        with pytest.raises(PublishError, match="cancelled"):
            loudness_db.mark_item(db, 1, deadline=_deadline(), cancel_check=lambda: True)
        assert not any(path.startswith("/v1/loudness/") for path in session.paths)


def _plex_serving_the_node(folder: Path) -> MagicMock:
    """A Plex whose API serves what the node's database holds: its identity, and each item's audio streams."""

    def query(path: str):
        if path == "/identity":
            return Element("MediaContainer", machineIdentifier=MACHINE, version=VERSION)
        conn = sqlite3.connect(plex_db.plex_db_path(str(folder)))
        try:
            rows = conn.execute(
                'SELECT ms.id, ms."index", ms.extra_data FROM media_streams ms WHERE ms.stream_type_id = 2'
            ).fetchall()
        finally:
            conn.close()
        container = Element("MediaContainer")
        part = Element("Part", id="1", file=FILE)
        for stream_id, index, extra in rows:
            fields = plex_db.decode_extra_data(extra)[0]
            served = {key[3:]: value for key, value in fields.items() if key.startswith("ln:")}
            part.append(
                Element(
                    "Stream",
                    id=str(stream_id),
                    index=str(index),
                    streamType="2",
                    canNormalizeLoudness="1" if served else "0",
                    **served,
                )
            )
        container.append(part)
        return container

    server = MagicMock()
    server._connect.return_value.query.side_effect = query
    return server


class TestTheLoudnessJobThroughTheNode:
    def test_a_file_is_analysed_here_and_written_there(self, node, folder, tmp_path):
        from media_preview_generator.loudness import job
        from media_preview_generator.output.plex_hash import calculate_plex_hash
        from media_preview_generator.processing.types import ProcessableItem
        from media_preview_generator.servers.base import Library, ServerConfig, ServerType

        media = tmp_path / "media" / Path(FILE).name
        media.parent.mkdir()
        media.write_bytes(b"x")
        with sqlite3.connect(plex_db.plex_db_path(str(folder))) as conn:
            conn.execute("UPDATE media_parts SET hash = ?, size = ? WHERE id = 1", (calculate_plex_hash(str(media)), 1))
        conn.close()
        cfg = ServerConfig(
            id="plex1",
            type=ServerType.PLEX,
            name="Plex",
            enabled=True,
            url="http://plex",
            auth={},
            libraries=[Library(id="4", name="TV", remote_paths=("/data/kids",), enabled=True, kind="show")],
            path_mappings=[{"remote_prefix": "/data/kids", "local_prefix": str(media.parent)}],
            loudness={"enabled": True, "library_ids": None},
        )
        db, _ = _remote(node, _plex_serving_the_node(folder))
        ctx = job.LoudnessContext(registry=MagicMock(), ffmpeg="ffmpeg")
        with (
            patch.object(job, "create_loudness_db", return_value=db),
            patch.object(job, "owners", return_value=[cfg]),
            patch.object(job.analyze, "run", return_value=FIELDS) as run,
        ):
            outcome = job.process_item(ProcessableItem(canonical_path=str(media), server_id="", title="ep"), ctx=ctx)

        assert [c.args[2] for c in run.call_args_list] == [1, 2]
        assert outcome.outcome_key == job.WRITTEN, outcome.publisher_rows
        assert outcome.publisher_rows[0]["message"] == "2 stream(s), item marked analysed"
        assert _extra(folder, "media_streams", 11) == PLEX_ANALYSED
        assert json.loads(_extra(folder, "metadata_items", 1))[loudness_db.VERSION_FIELD] == "0.02"


class TestTheNodeProvesWhichPlexItWrites:
    def test_a_node_beside_another_plex_writes_nothing(self, node, folder):
        db, _ = _remote(node, _plex(machine="plex-b"))
        with pytest.raises(PublishError) as ei:
            loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline())
        assert ei.value.state is Capability.MISCONFIGURED
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_an_untested_plex_version_writes_nothing(self, node, folder):
        db, _ = _remote(node, _plex(version="1.99.0.1-new"))
        with pytest.raises(PublishError) as ei:
            loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline())
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_a_plex_that_cant_say_who_it_is_is_unreachable_and_nothing_is_sent(self, node):
        server = MagicMock()
        server._connect.side_effect = OSError("down")
        db, session = _remote(node, server)
        with pytest.raises(PublishError) as ei:
            loudness_db.read_streams(db, [FILE], deadline=_deadline())
        assert ei.value.state is Capability.UNREACHABLE
        assert session.paths == []

    @linux_mount_table
    def test_a_database_on_a_network_share_on_the_node_writes_nothing(self, folder, tmp_path):
        node = node_app(str(folder), mountinfo_path=_mountinfo(tmp_path, fs="nfs4")).test_client()
        db, _ = _remote(node)
        assert db.file_checks(deadline=_deadline()).state is Capability.NEEDS_LOCAL_DB
        with pytest.raises(PublishError) as ei:
            loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline())
        assert ei.value.state is Capability.NEEDS_LOCAL_DB
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_a_write_with_no_identity_at_all_is_refused_by_the_node(self, node, folder):
        body = {"stream_id": 11, "fields": FIELDS, "deadline_s": 5}
        response = node.post("/v1/loudness/write", json=body, headers=AUTH)
        assert response.status_code == 409
        assert response.get_json()["error"]["state"] == Capability.MISCONFIGURED.value
        assert _extra(folder, "media_streams", 11) == BEFORE


class TestAnOlderHelper:
    def test_a_helper_without_loudness_gets_an_update_message_and_no_request(self, node, folder, monkeypatch):
        monkeypatch.setattr(plex_db_role, "CAPABILITIES", [CHAPTER_CAPABILITY])
        db, session = _remote(node)
        with pytest.raises(PublishError) as ei:
            loudness_db.write_stream(db, 11, FIELDS, deadline=_deadline())
        assert ei.value.state is Capability.PLUGIN_OUTDATED
        assert "1.2.0 or newer" in str(ei.value) and plex_db_role.AGENT_VERSION in str(ei.value)
        assert session.paths == ["/v1/ping"]
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_its_readiness_says_to_update_it(self, node, monkeypatch):
        monkeypatch.setattr(plex_db_role, "CAPABILITIES", [CHAPTER_CAPABILITY])
        db, _ = _remote(node)
        report = db.file_checks(deadline=_deadline())
        assert report.state is Capability.PLUGIN_OUTDATED and "Update the node" in report.message

    def test_an_unreachable_helper_is_a_retryable_unavailable_not_a_crash(self, node):
        db, session = _remote(node)
        session.stopped = True
        report = db.file_checks(deadline=_deadline())
        assert report.state is Capability.AGENT_UNAVAILABLE


class TestTheRouteRefusesWhatIsntAnOperation:
    @pytest.mark.parametrize(
        ("operation", "body"),
        [
            ("drop_table", {}),
            ("streams", {"plex_paths": []}),
            ("streams", {"plex_paths": "/data/x.mkv"}),
            ("streams", {"plex_paths": ["/x"] * 65}),
            ("snapshot", {"metadata_item_id": "1"}),
            ("snapshot", {"metadata_item_id": True}),
            ("write", {"stream_id": 11, "fields": {"ln:loudness": -23}}),
            ("write", {"stream_id": 0, "fields": FIELDS}),
            ("write", {"stream_id": 11, "fields": FIELDS, "expected": [1, 2]}),
            ("mark", {"metadata_item_id": 1, "expected": [[1]]}),
            ("mark", {"metadata_item_id": 1, "skipped": [["h", "1", "aac"]]}),
        ],
    )
    def test_a_body_that_isnt_what_it_should_be_is_refused(self, node, folder, operation, body):
        payload = {"machine_identifier": MACHINE, "pms_version": VERSION, **body}
        assert node.post(f"/v1/loudness/{operation}", json=payload, headers=AUTH).status_code == 400
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_an_incomplete_measurement_is_refused_on_the_node_too(self, node, folder):
        fields = {k: v for k, v in FIELDS.items() if k != "ln:peak"}
        body = {"machine_identifier": MACHINE, "pms_version": VERSION, "stream_id": 11, "fields": fields}
        response = node.post("/v1/loudness/write", json=body, headers=AUTH)
        assert response.status_code == 409 and "incomplete" in response.get_json()["error"]["message"]
        assert _extra(folder, "media_streams", 11) == BEFORE


class TestTheCodec:
    def test_a_stream_round_trips_exactly(self, node, folder, tmp_path):
        streams, _ = loudness_db.read_streams(_local(folder, tmp_path), [FILE], deadline=_deadline())
        for stream in streams:
            assert loudness_db.stream_from_json(json.loads(json.dumps(loudness_db.stream_to_json(stream)))) == stream

    def test_a_snapshot_round_trips_exactly(self, folder, tmp_path):
        snapshot = loudness_db.read_item_snapshot(_local(folder, tmp_path), 1, deadline=_deadline())
        wire = json.loads(json.dumps(loudness_db.snapshot_to_json(snapshot)))
        assert loudness_db.snapshot_from_json(wire) == snapshot

    @pytest.mark.parametrize(
        "raw", [None, [], [1] * 15, [{"x": 1}] * 15, ["1", 1, "aac", None, 1, "/f", 1, False, 1, 1, None, 1, 1, 1, 2]]
    )
    def test_anything_else_is_not_a_stream(self, raw):
        with pytest.raises(ValueError):
            loudness_db.stream_from_json(raw)

    def test_a_changed_source_keeps_its_type_across_the_wire(self):
        error = plex_remote.error_from_json(plex_remote.error_to_json(loudness_db.SourceChangedError("changed")))
        assert type(error) is loudness_db.SourceChangedError and str(error) == "changed"


class TestTheUndoRecordSurvivesAnUpdate:
    """The node keeps the undo record in ``CONFIG_DIR``; inside a container that folder has to be a mount of its own."""

    @staticmethod
    def _check(folder: Path, tmp_path: Path, *, root_fs: str, config_mounted: bool) -> object:
        config_dir = Path(loudness_db.write_log_path()).parent
        extra = f"40 22 0:50 / {config_dir} rw - ext4 /dev/sdb1 rw\n" if config_mounted else ""
        node = node_app(str(folder), mountinfo_path=_mountinfo(tmp_path, fs=root_fs, extra_lines=extra)).test_client()
        db, _ = _remote(node)
        return db.capability(deadline=_deadline())

    @pytest.mark.parametrize(
        ("root_fs", "config_mounted", "ready"),
        [
            ("overlay", False, False),
            ("overlay", True, True),
            ("ext4", False, True),
        ],
        ids=["container-layer", "container-volume", "host-disk"],
    )
    @linux_mount_table
    def test_the_readiness_check(self, folder, tmp_path, root_fs, config_mounted, ready):
        report = self._check(folder, tmp_path, root_fs=root_fs, config_mounted=config_mounted)
        if ready:
            assert report.state is Capability.READY
        else:
            assert report.state is Capability.MISCONFIGURED
            config_dir = Path(loudness_db.write_log_path()).parent
            assert report.message.startswith(f"Mount a volume at {config_dir} on this Plex database node")
            assert "undo record survives updates" in report.message

    def test_a_folder_inside_the_volume_counts_as_on_it(self, folder, tmp_path, monkeypatch):
        volume = tmp_path / "node-config"
        monkeypatch.setenv("CONFIG_DIR", str(volume / "sub"))
        extra = f"40 22 0:50 / {volume} rw - ext4 /dev/sdb1 rw\n"
        assert (
            plex_db_role.undo_record_problem(mountinfo_path=_mountinfo(tmp_path, fs="overlay", extra_lines=extra)) == ""
        )

    def test_windows_keeps_it_under_localappdata_and_is_never_asked_for_a_volume(self, folder, tmp_path, monkeypatch):
        monkeypatch.setattr(plex_db_role, "_ON_WINDOWS", True)
        assert self._check(folder, tmp_path, root_fs="overlay", config_mounted=False).state is Capability.READY

    def test_an_unreadable_mountinfo_is_not_a_refusal(self, tmp_path):
        assert plex_db_role.undo_record_problem(mountinfo_path=str(tmp_path / "missing")) == ""

    def test_the_image_declares_the_volume(self):
        dockerfile = (Path(__file__).parents[2] / "nodes/plex-db/Dockerfile").read_text()
        assert "\nVOLUME /config\n" in dockerfile


class TestAWriteWaitsForPlexInSlices:
    """The node can't see the job's cancel or the source file, so the app sends short waits and checks between them."""

    @staticmethod
    def _plex_holds_the_write_lock(folder: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(plex_db.plex_db_path(str(folder)), isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def test_a_cancel_during_a_long_lock_wait_stops_within_one_slice_and_writes_nothing(self, node, folder):
        db, session = _remote(node)
        plex = self._plex_holds_the_write_lock(folder)
        calls = []

        def cancel_check() -> bool:
            calls.append(time.monotonic())
            return len(calls) > 1

        started = time.monotonic()
        try:
            with pytest.raises(PublishError, match="cancelled"):
                loudness_db.write_stream(db, 11, FIELDS, deadline=time.monotonic() + 60, cancel_check=cancel_check)
        finally:
            plex.rollback()
            plex.close()
        assert time.monotonic() - started < plex_db.WAIT_SLICE_S + 2.0
        assert session.paths.count("/v1/loudness/write") == 1
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_a_source_change_between_slices_stops_the_write(self, node, folder, tmp_path):
        db, session = _remote(node)
        source = tmp_path / "local.mkv"
        source.write_bytes(b"old")
        fingerprint = loudness_db.SourceFingerprint.read(str(source))
        plex = self._plex_holds_the_write_lock(folder)
        calls = []

        def cancel_check() -> bool:
            calls.append(1)
            if len(calls) == 2:
                source.write_bytes(b"changed while Plex held the lock")
            return False

        try:
            with pytest.raises(loudness_db.SourceChangedError):
                loudness_db.write_stream(
                    db, 11, FIELDS, deadline=time.monotonic() + 60, source=fingerprint, cancel_check=cancel_check
                )
        finally:
            plex.rollback()
            plex.close()
        assert session.paths.count("/v1/loudness/write") == 1
        assert _extra(folder, "media_streams", 11) == BEFORE

    def test_the_write_goes_through_once_plex_lets_go(self, node, folder):
        db, session = _remote(node)
        plex = self._plex_holds_the_write_lock(folder)

        def cancel_check() -> bool:
            if session.paths.count("/v1/loudness/write") == 1 and plex.in_transaction:
                plex.rollback()
            return False

        try:
            assert loudness_db.write_stream(db, 11, FIELDS, deadline=time.monotonic() + 60, cancel_check=cancel_check)
        finally:
            plex.close()
        assert session.paths.count("/v1/loudness/write") == 2
        assert _extra(folder, "media_streams", 11) != BEFORE

    def test_each_attempt_asks_the_node_for_at_most_one_slice(self, node, folder):
        db, session = _remote(node)
        plex = self._plex_holds_the_write_lock(folder)
        try:
            with pytest.raises(plex_db.DatabaseBusyError) as caught:
                loudness_db.mark_item(db, 1, deadline=time.monotonic() + 2.5)
        finally:
            plex.rollback()
            plex.close()
        assert session.paths.count("/v1/loudness/mark") >= 2
        assert session.bodies["/v1/loudness/mark"]["deadline_s"] <= plex_db.WAIT_SLICE_S
        # The message counts the whole wait, not the last slice.
        assert "for 2 s" in str(caught.value) or "for 3 s" in str(caught.value)
        assert _extra(folder, "metadata_items", 1) is None
