"""Read counts for the previews + chapter thumbnails check of a file that is already done.

A steady-state bulk-scan check must stay cheap: these are upper bounds, so a later change cannot quietly
bring the slow reads back. Real SQLite, real image files and a real journal; only Plex's HTTP API is faked.
"""

import contextlib
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageFile

from media_preview_generator.markers.publishers import plex_chapters as plex_chapters_module
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.plex_chapters import LocalChapters
from media_preview_generator.output.journal import write_meta
from media_preview_generator.output.plex_bundle import PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters
from media_preview_generator.processing.multi_server import MultiServerStatus, process_canonical_path
from media_preview_generator.servers import ServerRegistry
from media_preview_generator.servers.plex import PlexServer
from media_preview_generator.servers.plex_chapters import Chapter, ChapterTarget

VERSION = "1.43.4.10903-e5521bd8c"
MACHINE = "synthetic-server"


class _Counts:
    def __init__(self) -> None:
        self.source_stats = 0
        self.http: list[str] = []
        self.db_connects = 0
        self.guards = 0
        self.schema_checks = 0
        self.jpeg_decodes = 0

    def as_dict(self) -> dict:
        return {
            "source_stats": self.source_stats,
            "http": list(self.http),
            "db_connects": self.db_connects,
            "guards": self.guards,
            "schema_checks": self.schema_checks,
            "jpeg_decodes": self.jpeg_decodes,
        }


class _FakePlex:
    def __init__(self, counts: _Counts) -> None:
        self._counts = counts

    def query(self, path: str):
        self._counts.http.append(path)
        return {"machineIdentifier": MACHINE, "version": VERSION}


@pytest.fixture
def steady_state(tmp_path, mock_config, monkeypatch):
    """A bulk-scan Plex movie whose BIF, chapter images and chapter registration are all done."""
    source = tmp_path / "movies" / "movie.mkv"
    source.parent.mkdir()
    source.write_bytes(b"stable video bytes" * 5000)
    bundle_hash = calculate_plex_hash(source)
    plex_folder = tmp_path / "Plex Media Server"
    db = Path(plex_db.plex_db_path(str(plex_folder)))
    db.parent.mkdir(parents=True)
    (plex_folder / "Preferences.xml").write_text(f'<Preferences ProcessedMachineIdentifier="{MACHINE}"/>')
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE tags(id INTEGER PRIMARY KEY,tag_type INTEGER);
            CREATE TABLE taggings(id INTEGER PRIMARY KEY,metadata_item_id INTEGER,tag_id INTEGER,
                "index" INTEGER,time_offset INTEGER,end_time_offset INTEGER,thumb_url TEXT,extra_data TEXT);
            CREATE TABLE media_items(id INTEGER PRIMARY KEY,metadata_item_id INTEGER,deleted_at INTEGER,proxy_type INTEGER);
            CREATE TABLE media_parts(id INTEGER PRIMARY KEY,media_item_id INTEGER,file TEXT,hash TEXT,size INTEGER,
                updated_at INTEGER,deleted_at INTEGER,extra_data TEXT);
            INSERT INTO tags VALUES(9,9);
            INSERT INTO media_items VALUES(10,1,NULL,NULL);
            INSERT INTO taggings VALUES(1,1,9,1,0,1000,'',NULL);
            INSERT INTO taggings VALUES(2,1,9,2,1000,2000,'',NULL);
        """)
        conn.execute(
            "INSERT INTO media_parts(id,media_item_id,file,hash,size,updated_at) VALUES(20,10,?,?,?,100)",
            (str(source), bundle_hash, source.stat().st_size),
        )
        conn.commit()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text("36 25 0:32 / / rw,relatime - ext4 /dev/sda1 rw\n")
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda *_a, **_k: True)
    monkeypatch.setattr(
        plex_chapters_module,
        "LocalChapters",
        lambda folder: LocalChapters(folder, mountinfo_path=str(mountinfo)),
    )

    registry = ServerRegistry.from_settings(
        [
            {
                "id": "plex",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": "http://plex.invalid",
                "auth": {"token": "test"},
                "libraries": [{"id": "1", "name": "Movies", "remote_paths": [str(source.parent)], "enabled": True}],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": str(plex_folder),
                    "chapter_thumbnails": True,
                },
            }
        ]
    )
    mock_config.webhook_source = None
    mock_config.webhook_paths = None
    mock_config.path_mappings = []

    counts = _Counts()
    monkeypatch.setattr(PlexServer, "_connect", lambda self: _FakePlex(counts))
    part = (bundle_hash, str(source), source.stat().st_size)
    monkeypatch.setattr(
        PlexServer, "get_bundle_parts", lambda self, item_id, quiet=False: counts.http.append("/tree") or [part]
    )

    bif = PlexBundleAdapter.bundle_bif_path(str(plex_folder), bundle_hash)
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    write_meta([bif], str(source), source_fingerprint=get_source_fingerprint(source))

    # Done chapter images and manifest, produced by the real publisher with extraction and registration stubbed.
    target = ChapterTarget(
        1, 10, 20, bundle_hash, str(source), source.stat().st_size, 100,
        (Chapter(1, 0, 1000, "", 1, 9), Chapter(2, 1000, 2000, "", 2, 9)),
        MACHINE,
    )  # fmt: skip
    plan = chapters.ChapterPlan(
        MagicMock(),
        str(source),
        get_source_fingerprint(source),
        bif.parent.parent / "Chapters",
        {"version": 1, "width": 1280, "quality": 4, "tonemap": "hable"},
        target,
        chapters.ChapterOutcome("queued", total=2),
    )
    monkeypatch.setattr(chapters.MediaInfo, "parse", lambda _path: object())
    monkeypatch.setattr(
        chapters,
        "extract_chapter_frame",
        lambda _source, _start, path, *_a, **_k: Image.new("RGB", (1280, 720), "red").save(path, "JPEG"),
    )
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", MagicMock())
    assert chapters.publish_chapters(plan, mock_config).status == "ready"
    manifest = json.loads(plan.manifest_path.read_text())
    with contextlib.closing(sqlite3.connect(db, isolation_level=None)) as conn:
        for index, entry in manifest["images"].items():
            url = f"/library/media/10/chapterImages/{index}?mpgChapter={entry['sha256']}"
            conn.execute('UPDATE taggings SET thumb_url=? WHERE "index"=?', (url, int(index)))

    # Count from here on.
    counts = _Counts()
    monkeypatch.setattr(PlexServer, "_connect", lambda self: _FakePlex(counts))
    monkeypatch.setattr(
        PlexServer, "get_bundle_parts", lambda self, item_id, quiet=False: counts.http.append("/tree") or [part]
    )
    real_stat = os.stat

    def counting_stat(path, *args, **kwargs):
        if str(path) == str(source):
            counts.source_stats += 1
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", counting_stat)

    real_connect = plex_db.LocalPlexDb._connect

    def counting_connect(self, **kwargs):
        counts.db_connects += 1
        return real_connect(self, **kwargs)

    monkeypatch.setattr(plex_db.LocalPlexDb, "_connect", counting_connect)
    real_guard = LocalChapters._guard
    monkeypatch.setattr(
        LocalChapters, "_guard", lambda self, *a, **k: (setattr(counts, "guards", counts.guards + 1), real_guard(self, *a, **k))[1]
    )  # fmt: skip
    real_schema = LocalChapters._schema
    monkeypatch.setattr(
        LocalChapters,
        "_schema",
        staticmethod(lambda conn: (setattr(counts, "schema_checks", counts.schema_checks + 1), real_schema(conn))[1]),
    )
    real_load = ImageFile.ImageFile.load

    def counting_load(self, *args, **kwargs):
        counts.jpeg_decodes += 1
        return real_load(self, *args, **kwargs)

    monkeypatch.setattr(ImageFile.ImageFile, "load", counting_load)

    def check():
        return process_canonical_path(
            str(source), registry, mock_config, check_only=True, item_id_by_server={"plex": "1"}
        )

    return check, counts


def test_done_file_check_reads_little_when_nothing_is_left_to_do(steady_state):
    check, counts = steady_state

    result = check()

    assert result.status is MultiServerStatus.SKIPPED
    # Before: 8 source stats, 1 /identity, 1 /tree, 1 file check, 1 schema check, 2 JPEG decodes. One of the six is
    # the preview half's own stat after chapter planning (a file replaced meanwhile is judged as it is then).
    assert counts.source_stats <= 6
    assert counts.http == ["/identity", "/tree"]
    assert (counts.db_connects, counts.guards, counts.schema_checks) == (1, 1, 1)
    assert counts.jpeg_decodes == 0


def test_second_file_reuses_plex_identity_and_database_checks(steady_state):
    check, counts = steady_state

    check()
    check()

    assert counts.http == ["/identity", "/tree", "/tree"]
    assert (counts.guards, counts.schema_checks) == (1, 1)
    # The database is opened per file: holding it between files would block Plex's own writers.
    assert counts.db_connects == 2
    assert counts.source_stats <= 12
    assert counts.jpeg_decodes == 0


def test_expired_checks_are_asked_again(steady_state, monkeypatch):
    check, counts = steady_state
    monkeypatch.setattr(plex_chapters_module, "_RECENT_CHECK_TTL_S", 0.0)

    check()
    check()

    assert counts.http.count("/identity") == 2
    assert (counts.guards, counts.schema_checks) == (2, 2)


def _plex_double(tmp_path, connect=None):
    api = MagicMock()
    api.query.return_value = MagicMock(
        get=lambda key, default=None: {"machineIdentifier": MACHINE, "version": VERSION}.get(key, default)
    )
    server = MagicMock()
    server._connect = connect or (lambda: api)
    server._server_config = MagicMock(markers=None, output={"plex_config_folder": str(tmp_path)})
    return server, api


def test_a_write_after_a_cached_read_asks_plex_for_its_identity_again(tmp_path, monkeypatch):
    server, api = _plex_double(tmp_path)
    for _ in range(3):
        plex_chapters_module._context(server, reuse_recent=True)
    assert api.query.call_count == 1

    backend = MagicMock()
    monkeypatch.setattr(plex_chapters_module, "LocalChapters", lambda _folder: backend)
    monkeypatch.setattr(plex_chapters_module, "verify_chapters", MagicMock())
    target = MagicMock(machine_identifier=MACHINE)
    plex_chapters_module.register_chapters(server, target, {1: "rev"})

    assert api.query.call_args_list[-1].args == ("/identity",)
    assert api.query.call_count == 2
    backend.register.assert_called_once()


def test_a_failed_connection_is_reported_even_with_a_recent_identity(tmp_path):
    server, api = _plex_double(tmp_path)
    plex_chapters_module._context(server, reuse_recent=True)
    server._connect = MagicMock(side_effect=OSError("connection refused"))

    with pytest.raises(plex_chapters_module.ChapterError) as caught:
        plex_chapters_module._context(server, reuse_recent=True)

    assert caught.value.code == "registration"
    assert api.query.call_count == 1
