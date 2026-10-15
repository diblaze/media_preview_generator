"""Node access: app path -> node path, the roots guard, the same-file check, and the nearby listing."""

from __future__ import annotations

import os

import pytest

from media_preview_generator.nodes import protocol
from media_preview_generator.nodes.protocol import PathMapping
from media_preview_generator.nodes.server import access


@pytest.fixture
def media(tmp_path):
    root = tmp_path / "media"
    (root / "Movies").mkdir(parents=True)
    movie = root / "Movies" / "a.mkv"
    movie.write_bytes(b"x" * 100)
    (tmp_path / "secret.txt").write_text("outside")
    return root


# A real library file's mtime as ZFS / NFS 4.2 report it, with digits below 100 ns.
APP_MTIME_NS = 1771246996454097483


def _rows(root) -> list[PathMapping]:
    return [PathMapping("/data", str(root))]


def _stat(path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


class TestResolve:
    def test_mapped_file_with_matching_identity_resolves(self, media):
        size, mtime_ns = _stat(media / "Movies" / "a.mkv")

        got = access.resolve("/data/Movies/a.mkv", size, mtime_ns, _rows(media))

        assert got == os.path.realpath(media / "Movies" / "a.mkv")

    def test_none_size_and_mtime_skip_the_identity_check(self, media):
        assert access.resolve("/data/Movies/a.mkv", None, None, _rows(media)).endswith("a.mkv")

    def test_unmapped(self, media):
        with pytest.raises(access.NotVisible) as caught:
            access.resolve("/elsewhere/a.mkv", None, None, _rows(media))

        assert caught.value.reason == protocol.NOT_VISIBLE_UNMAPPED
        assert caught.value.node_path is None

    def test_dotdot_traversal_is_outside_roots(self, media):
        with pytest.raises(access.NotVisible) as caught:
            access.resolve("/data/../secret.txt", None, None, _rows(media))

        assert caught.value.reason == protocol.NOT_VISIBLE_OUTSIDE_ROOTS

    def test_symlink_escaping_the_root_is_outside_roots(self, media, tmp_path):
        (media / "Movies" / "link.mkv").symlink_to(tmp_path / "secret.txt")

        with pytest.raises(access.NotVisible) as caught:
            access.resolve("/data/Movies/link.mkv", None, None, _rows(media))

        assert caught.value.reason == protocol.NOT_VISIBLE_OUTSIDE_ROOTS

    def test_symlink_into_another_mapped_root_is_allowed(self, media, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        (other / "b.mkv").write_bytes(b"y")
        (media / "Movies" / "b.mkv").symlink_to(other / "b.mkv")
        rows = [*_rows(media), PathMapping("/other", str(other))]

        assert access.resolve("/data/Movies/b.mkv", 1, None, rows) == os.path.realpath(other / "b.mkv")

    def test_missing(self, media):
        with pytest.raises(access.NotVisible) as caught:
            access.resolve("/data/Movies/nope.mkv", None, None, _rows(media))

        assert caught.value.reason == protocol.NOT_VISIBLE_MISSING
        assert caught.value.node_path == str(media / "Movies" / "nope.mkv")

    def test_a_folder_is_not_a_file(self, media):
        with pytest.raises(access.NotVisible) as caught:
            access.resolve("/data/Movies", None, None, _rows(media))

        assert caught.value.reason == protocol.NOT_VISIBLE_MISSING

    def test_different_size_is_a_different_file(self, media):
        _, mtime_ns = _stat(media / "Movies" / "a.mkv")

        with pytest.raises(access.NotVisible) as caught:
            access.resolve("/data/Movies/a.mkv", 99, mtime_ns, _rows(media))

        assert caught.value.reason == protocol.NOT_VISIBLE_DIFFERENT_FILE

    def test_an_mtime_2_seconds_off_is_a_different_file(self, media):
        size, mtime_ns = _stat(media / "Movies" / "a.mkv")

        with pytest.raises(access.NotVisible) as caught:
            access.resolve("/data/Movies/a.mkv", size, mtime_ns + access.MTIME_TOLERANCE_NS, _rows(media))

        assert caught.value.reason == protocol.NOT_VISIBLE_DIFFERENT_FILE

    @pytest.mark.parametrize(
        "node_mtime_ns",
        [
            APP_MTIME_NS // 100 * 100,  # SMB2/3: 100 ns
            APP_MTIME_NS // 10**9 * 10**9,  # sshfs, SMB1: whole seconds
            APP_MTIME_NS // (2 * 10**9) * (2 * 10**9),  # FAT: 2 seconds
            APP_MTIME_NS + access.MTIME_TOLERANCE_NS - 1,
        ],
        ids=["smb-100ns", "whole-seconds", "fat-2s", "just-under-2s"],
    )
    def test_the_same_file_through_a_coarser_timestamp_filesystem_is_the_same_file(self, media, node_mtime_ns):
        movie = media / "Movies" / "a.mkv"
        os.utime(movie, ns=(node_mtime_ns, node_mtime_ns))

        got = access.resolve("/data/Movies/a.mkv", 100, APP_MTIME_NS, _rows(media))

        assert got == os.path.realpath(movie)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
    def test_unreadable(self, media):
        movie = media / "Movies" / "a.mkv"
        movie.chmod(0)
        try:
            with pytest.raises(access.NotVisible) as caught:
                access.resolve("/data/Movies/a.mkv", None, None, _rows(media))
        finally:
            movie.chmod(0o644)

        assert caught.value.reason == protocol.NOT_VISIBLE_UNREADABLE

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads any folder")
    def test_untraversable_folder_is_unreadable(self, media):
        (media / "Movies").chmod(0)
        try:
            with pytest.raises(access.NotVisible) as caught:
                access.resolve("/data/Movies/a.mkv", None, None, _rows(media))
        finally:
            (media / "Movies").chmod(0o755)

        assert caught.value.reason == protocol.NOT_VISIBLE_UNREADABLE

    def test_longest_app_prefix_wins(self, tmp_path):
        broad, narrow = tmp_path / "broad", tmp_path / "narrow"
        (broad / "tv").mkdir(parents=True)
        narrow.mkdir()
        (broad / "tv" / "x.mkv").write_bytes(b"wrong")
        (narrow / "x.mkv").write_bytes(b"right")
        rows = [PathMapping("/data", str(broad)), PathMapping("/data/tv", str(narrow))]

        assert access.resolve("/data/tv/x.mkv", None, None, rows) == os.path.realpath(narrow / "x.mkv")


class TestNearby:
    def test_lists_the_deepest_existing_folder(self, media):
        found = access.nearby(str(media / "Movies" / "Sub" / "nope.mkv"), _rows(media))

        assert found == (str(media / "Movies"), ["a.mkv"])

    def test_caps_the_listing_at_50_names(self, media):
        for n in range(60):
            (media / "Movies" / f"f{n:02}.mkv").write_bytes(b"")

        _, names = access.nearby(str(media / "Movies" / "nope.mkv"), _rows(media))

        assert len(names) == access.NEARBY_MAX_NAMES

    def test_a_folder_named_like_the_path_survives_the_cap_though_it_sorts_late(self, tmp_path):
        rows = [PathMapping("/data", str(tmp_path / "share" / "not-there"))]
        for n in range(60):
            (tmp_path / "share" / f"A{n:02}").mkdir(parents=True)
        (tmp_path / "share" / "Movies").mkdir()

        _, names = access.nearby(str(tmp_path / "share" / "not-there" / "movies" / "a.mkv"), rows)

        assert len(names) == access.NEARBY_MAX_NAMES and names[0] == "Movies"

    def test_beside_a_missing_node_folder_only_folder_names_are_listed(self, tmp_path):
        rows = [PathMapping("/data", str(tmp_path / "media" / "not-there"))]
        (tmp_path / "media" / "Movies" / "private").mkdir(parents=True)
        (tmp_path / "media" / "private.txt").write_text("")

        found = access.nearby(str(tmp_path / "media" / "not-there" / "Movies" / "a.mkv"), rows)

        assert found == (str(tmp_path / "media"), ["Movies"])

    def test_nothing_above_the_node_folders_parent_is_listed(self, tmp_path):
        rows = [PathMapping("/data", str(tmp_path / "media" / "gone" / "not-there"))]
        (tmp_path / "media" / "Movies").mkdir(parents=True)

        assert access.nearby(str(tmp_path / "media" / "gone" / "not-there" / "a.mkv"), rows) is None

    def test_an_existing_folder_outside_the_roots_is_not_listed(self, tmp_path):
        (tmp_path / "media" / "Movies").mkdir(parents=True)
        (tmp_path / "outside").mkdir()
        os.symlink(tmp_path / "outside", tmp_path / "media" / "Movies" / "link")
        rows = [PathMapping("/data", str(tmp_path / "media" / "Movies"))]

        assert access.nearby(str(tmp_path / "media" / "Movies" / "link" / "Sub" / "a.mkv"), rows) is None
