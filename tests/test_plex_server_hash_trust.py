"""Bulk scans take Plex's bundle hash only when it provably describes this file."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.output import BifBundle, PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash

PLEX_HASH = "ab" + "c" * 38


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "Movie (2020).mkv"
    path.write_bytes(b"current media" * 20000)
    return path


def _paths(media: Path, parts, *, trust: bool = True, item_id: str | None = "42", server=True):
    fake = MagicMock()
    fake.get_bundle_parts.return_value = parts
    bundle = BifBundle(
        canonical_path=str(media),
        frame_dir=media.parent,
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=0,
        trust_server_hash=trust,
    )
    adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
    return adapter.compute_output_paths(bundle, fake if server else None, item_id)[0], fake


def _bundle_hash(path: Path) -> str:
    return path.parts[-5] + path.parts[-4].removesuffix(".bundle")


def _size(media: Path) -> int:
    return media.stat().st_size


class TestServerHashTrust:
    def test_matching_size_and_name_uses_plex_hash_without_reading_file(self, media, monkeypatch):
        monkeypatch.setattr(
            "media_preview_generator.output.plex_bundle.calculate_plex_hash",
            MagicMock(side_effect=AssertionError("must not read the file")),
        )
        path, plex = _paths(media, [(PLEX_HASH, f"/plex/{media.name}", _size(media))])
        assert _bundle_hash(path) == PLEX_HASH
        plex.get_bundle_parts.assert_called_once_with("42", quiet=True)

    def test_size_mismatch_reads_file(self, media):
        path, _ = _paths(media, [(PLEX_HASH, f"/plex/{media.name}", _size(media) + 1)])
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_no_plex_hash_yet_reads_file(self, media):
        path, _ = _paths(media, [])
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_other_filename_reads_file(self, media):
        path, _ = _paths(media, [(PLEX_HASH, "/plex/Other.mkv", _size(media))])
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_conflicting_hashes_read_file(self, media):
        parts = [(PLEX_HASH, f"/p/{media.name}", _size(media)), ("f" * 40, f"/q/{media.name}", _size(media))]
        path, _ = _paths(media, parts)
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_multi_version_picks_matching_size(self, media):
        parts = [("f" * 40, "/p/Movie (2020) 4K.mkv", 999), (PLEX_HASH, f"/p/{media.name}", _size(media))]
        path, _ = _paths(media, parts)
        assert _bundle_hash(path) == PLEX_HASH

    def test_windows_remote_path(self, media):
        path, _ = _paths(media, [(PLEX_HASH, f"D:\\Movies\\{media.name}", _size(media))])
        assert _bundle_hash(path) == PLEX_HASH

    @pytest.mark.parametrize("kwargs", [{"trust": False}, {"item_id": None}, {"server": False}])
    def test_webhook_and_path_only_never_ask_plex(self, media, kwargs):
        path, plex = _paths(media, [(PLEX_HASH, f"/plex/{media.name}", _size(media))], **kwargs)
        assert _bundle_hash(path) == calculate_plex_hash(media)
        plex.get_bundle_parts.assert_not_called()


class TestBundleParts:
    def test_tree_parts_carry_size(self):
        from xml.etree import ElementTree as ET

        from media_preview_generator.servers.plex import PlexServer

        xml = ET.fromstring(
            '<MediaContainer><MediaPart hash="h1" file="/a/b.mkv" size="123"/><MediaPart hash="h2" file="/a/c.mkv"/></MediaContainer>'
        )
        server = PlexServer.__new__(PlexServer)
        server._connect = MagicMock(return_value=MagicMock(query=MagicMock(return_value=xml)))
        assert server.get_bundle_parts("/library/metadata/7") == [("h1", "/a/b.mkv", 123), ("h2", "/a/c.mkv", -1)]
        assert server.get_bundle_metadata("7") == [("h1", "/a/b.mkv"), ("h2", "/a/c.mkv")]


TREE_HASH = "1" * 40
DB_HASH = "2" * 40


def _plex_with_database(
    tmp_path: Path, monkeypatch, rows, *, folder: bool = True, fs: str = "ext4", plex_holds_it: bool = True
):
    """A real PlexServer whose Plex config folder holds a WAL database (as Plex's is) with these
    ``(item, hash, file, size[, part deleted, item deleted])`` parts on a disk of type ``fs``, and whose /tree answers
    every item with TREE_HASH (counting the requests)."""
    import sqlite3
    from xml.etree import ElementTree as ET

    from media_preview_generator.markers.publishers import plex_db
    from media_preview_generator.markers.publishers.plex_db import plex_db_path
    from media_preview_generator.servers import plex as plex_mod
    from media_preview_generator.servers.base import ServerConfig, ServerType
    from media_preview_generator.servers.plex import PlexServer

    plex_folder = tmp_path / "plex"
    db = Path(plex_db_path(str(plex_folder)))
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE media_items (id INTEGER PRIMARY KEY, metadata_item_id INTEGER, deleted_at INTEGER)")
    conn.execute(
        "CREATE TABLE media_parts (id INTEGER PRIMARY KEY, media_item_id INTEGER, hash TEXT, file TEXT, size INTEGER, "
        "deleted_at INTEGER)"
    )
    for n, (item, bundle_hash, file_path, size, *deleted) in enumerate(rows, start=1):
        part_deleted, item_deleted = (deleted + [False, False])[:2]
        conn.execute("INSERT INTO media_items VALUES (?, ?, ?)", (n, item, 1 if item_deleted else None))
        conn.execute(
            "INSERT INTO media_parts VALUES (?, ?, ?, ?, ?, ?)",
            (n, n, bundle_hash, file_path, size, 1 if part_deleted else None),
        )
    conn.commit()
    conn.close()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"22 1 259:2 / / rw - {fs} /dev/root rw\n")
    monkeypatch.setattr(plex_mod, "_MOUNTINFO_PATH", str(mountinfo))
    # Plex holding the database open through this same file (no Plex process here to hold it).
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda path, deadline=None: plex_holds_it)
    cfg = ServerConfig(
        id="plex-1", type=ServerType.PLEX, name="Plex", enabled=True, url="http://plex.local:32400",
        auth={"token": "t", "method": "token"}, libraries=[],
        output={"adapter": "plex_bundle", "plex_config_folder": str(plex_folder) if folder else ""},
    )  # fmt: skip
    server = PlexServer(cfg)
    tree = ET.fromstring(f'<MediaContainer><MediaPart hash="{TREE_HASH}" file="/t/x.mkv" size="5"/></MediaContainer>')
    query = MagicMock(return_value=tree)
    server._connect = MagicMock(return_value=MagicMock(query=query))
    return server, query


def _age_read(server, seconds: float) -> None:
    server._db_parts_read_at -= seconds


TREE_PARTS = [(TREE_HASH, "/t/x.mkv", 5)]


class TestBundlePartsFromPlexDatabase:
    """Bulk scans (``quiet=True``) take parts from one read of Plex's database instead of a /tree request per file."""

    def test_bulk_lookups_read_the_database_once_and_never_ask_tree(self, tmp_path, monkeypatch):
        rows = [(7, DB_HASH, "/m/a.mkv", 100), (7, "3" * 40, "/m/a 4K.mkv", 200), (8, "4" * 40, "/m/b.mkv", 9)]
        server, tree = _plex_with_database(tmp_path, monkeypatch, rows)
        reads = MagicMock(wraps=server._read_parts_from_database)
        monkeypatch.setattr(server, "_read_parts_from_database", reads)
        assert server.get_bundle_parts("7", quiet=True) == [(DB_HASH, "/m/a.mkv", 100), ("3" * 40, "/m/a 4K.mkv", 200)]
        assert server.get_bundle_parts("/library/metadata/8", quiet=True) == [("4" * 40, "/m/b.mkv", 9)]
        tree.assert_not_called()
        assert reads.call_count == 1

    def test_a_part_without_a_hash_yet_is_left_out_as_tree_leaves_it_out(self, tmp_path, monkeypatch):
        server, tree = _plex_with_database(tmp_path, monkeypatch, [(7, None, "/m/a.mkv", 100), (7, "", "/m/b.mkv", 5)])
        assert server.get_bundle_parts("7", quiet=True) == []
        tree.assert_not_called()

    @pytest.mark.parametrize("deleted", [(True, False), (False, True)], ids=["part deleted", "media item deleted"])
    def test_a_deleted_part_is_never_offered(self, tmp_path, monkeypatch, deleted):
        """A replaced file's old part (same name and size, old hash) must not name the bundle: only the live part may,
        and with no live hash yet the file itself is hashed."""
        rows = [(7, "9" * 40, "/m/a.mkv", 100, *deleted), (7, None, "/m/a.mkv", 100)]
        server, _ = _plex_with_database(tmp_path, monkeypatch, rows)
        assert server.get_bundle_parts("7", quiet=True) == []

    def test_an_item_added_since_the_read_is_asked_with_tree(self, tmp_path, monkeypatch):
        server, tree = _plex_with_database(tmp_path, monkeypatch, [(7, DB_HASH, "/m/a.mkv", 100)])
        assert server.get_bundle_parts("9", quiet=True) == TREE_PARTS
        tree.assert_called_once_with("/library/metadata/9/tree")

    @pytest.mark.parametrize(
        "why", ["no folder set", "no database there", "nfs4", "cifs", "unknown filesystem", "plex holds another path"]
    )
    def test_without_a_local_readable_database_every_file_asks_tree(self, tmp_path, monkeypatch, why):
        """A database on a network share, or one Plex doesn't hold open through this same file (a FUSE pool's second
        view of it, a stale copy), is never opened: even a reader updates SQLite's shared index, which can't be kept
        right across machines or cached views and could damage Plex's database."""
        import sqlite3

        fs = {"nfs4": "nfs4", "cifs": "cifs", "unknown filesystem": "fuse.weird"}.get(why, "ext4")
        server, tree = _plex_with_database(
            tmp_path,
            monkeypatch,
            [(7, DB_HASH, "/m/a.mkv", 100)],
            folder=why != "no folder set",
            fs=fs,
            plex_holds_it=why != "plex holds another path",
        )
        if why == "no database there":
            from media_preview_generator.markers.publishers import plex_db

            Path(plex_db.plex_db_path(str(tmp_path / "plex"))).unlink()
        opened = MagicMock(side_effect=AssertionError("must not open Plex's database"))
        monkeypatch.setattr(sqlite3, "connect", opened)
        assert server.get_bundle_parts("7", quiet=True) == TREE_PARTS
        assert server.get_bundle_parts("7", quiet=True) == TREE_PARTS
        assert tree.call_count == 2
        opened.assert_not_called()

    def test_a_failed_read_is_made_again_once_its_ttl_passes(self, tmp_path, monkeypatch):
        from media_preview_generator.servers import plex as plex_mod

        server, tree = _plex_with_database(tmp_path, monkeypatch, [(7, DB_HASH, "/m/a.mkv", 100)])
        real_read = server._read_parts_from_database
        reads = MagicMock(side_effect=[None, real_read()])
        monkeypatch.setattr(server, "_read_parts_from_database", reads)
        assert server.get_bundle_parts("7", quiet=True) == TREE_PARTS
        _age_read(server, plex_mod._BUNDLE_PARTS_READ_TTL_S - 1)
        assert server.get_bundle_parts("7", quiet=True) == TREE_PARTS
        _age_read(server, 2)
        assert server.get_bundle_parts("7", quiet=True) == [(DB_HASH, "/m/a.mkv", 100)]
        assert reads.call_count == 2
        assert tree.call_count == 2

    def test_a_busy_database_falls_back_to_tree_within_the_short_deadline(self, tmp_path, monkeypatch):
        import time as real_time

        from media_preview_generator.markers.publishers import plex_db
        from media_preview_generator.servers import plex as plex_mod

        server, tree = _plex_with_database(tmp_path, monkeypatch, [(7, DB_HASH, "/m/a.mkv", 100)])
        monkeypatch.setattr(plex_mod, "_BUNDLE_PARTS_READ_DEADLINE_S", 0.2)
        holder = plex_db.LocalPlexDb(lambda: plex_db.plex_db_path(str(tmp_path / "plex")))
        result = {}

        def look_up():
            started = real_time.monotonic()
            result["parts"] = server.get_bundle_parts("7", quiet=True)
            result["took"] = real_time.monotonic() - started

        with holder._database(read_only=False, deadline=real_time.monotonic() + 5):  # a marker write holds it
            worker = threading.Thread(target=look_up)
            worker.start()
            worker.join(5)
        assert result["parts"] == TREE_PARTS
        assert result["took"] < 2

    def test_workers_arriving_during_a_read_never_wait_for_it(self, tmp_path, monkeypatch):
        """Only one worker reads; the others answer from /tree meanwhile, and everyone uses the read once made."""
        server, tree = _plex_with_database(tmp_path, monkeypatch, [(7, DB_HASH, "/m/a.mkv", 100)])
        real_read = server._read_parts_from_database
        reading, release = threading.Event(), threading.Event()

        def slow_read():
            reading.set()
            release.wait(5)
            return real_read()

        reads = MagicMock(side_effect=slow_read)
        monkeypatch.setattr(server, "_read_parts_from_database", reads)
        first = {}
        reader = threading.Thread(target=lambda: first.setdefault("parts", server.get_bundle_parts("7", quiet=True)))
        reader.start()
        assert reading.wait(5)
        others = [server.get_bundle_parts("7", quiet=True) for _ in range(3)]
        release.set()
        reader.join(5)
        assert others == [TREE_PARTS] * 3
        assert first["parts"] == [(DB_HASH, "/m/a.mkv", 100)]
        assert server.get_bundle_parts("7", quiet=True) == [(DB_HASH, "/m/a.mkv", 100)]
        assert reads.call_count == 1

    def test_a_lookup_that_isnt_a_bulk_scans_always_asks_tree(self, tmp_path, monkeypatch):
        server, tree = _plex_with_database(tmp_path, monkeypatch, [(7, DB_HASH, "/m/a.mkv", 100)])
        assert server.get_bundle_parts("7") == TREE_PARTS
        tree.assert_called_once()

    def test_the_read_is_made_again_once_older_than_its_ttl(self, tmp_path, monkeypatch):
        import sqlite3

        from media_preview_generator.markers.publishers.plex_db import plex_db_path
        from media_preview_generator.servers import plex as plex_mod

        server, tree = _plex_with_database(tmp_path, monkeypatch, [(7, DB_HASH, "/m/a.mkv", 100)])
        assert server.get_bundle_parts("7", quiet=True) == [(DB_HASH, "/m/a.mkv", 100)]
        conn = sqlite3.connect(plex_db_path(str(tmp_path / "plex")))
        conn.execute("UPDATE media_parts SET hash = ?", ("5" * 40,))
        conn.commit()
        conn.close()
        _age_read(server, plex_mod._BUNDLE_PARTS_READ_TTL_S - 1)
        assert server.get_bundle_parts("7", quiet=True) == [(DB_HASH, "/m/a.mkv", 100)]
        _age_read(server, 2)
        assert server.get_bundle_parts("7", quiet=True) == [("5" * 40, "/m/a.mkv", 100)]
        tree.assert_not_called()

    def test_the_bundle_path_from_the_database_is_the_one_tree_gives(self, tmp_path, monkeypatch, media):
        """End to end through the adapter: the same file, size and hash give the same bundle as /tree did."""
        server, tree = _plex_with_database(
            tmp_path, monkeypatch, [(42, PLEX_HASH, f"/plex/{media.name}", _size(media))]
        )
        bundle = BifBundle(
            canonical_path=str(media), frame_dir=media.parent, bif_path=None, frame_interval=10, width=320, height=180,
            frame_count=0, trust_server_hash=True,
        )  # fmt: skip
        path = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10).compute_output_paths(
            bundle, server, "42"
        )[0]
        assert _bundle_hash(path) == PLEX_HASH
        tree.assert_not_called()
