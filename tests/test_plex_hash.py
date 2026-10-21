"""Plex hash boundaries, bounded reads and source replacement protection."""

from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest

from media_preview_generator.output import plex_hash
from tests.conftest import CTIME_ONLY_CHANGES, SOURCE_CHANGES, change_ctime_only, change_source


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0da39a3ee5e6b4b0d3255bfef95601890afd80709"),
        (1, "15ba93c9db0cff93f52b521d7420e43f6eda2784f"),
        (65535, "65535fcdcbc0a3c69abbb021afa4a13ec39c4ad11cdb8"),
        (65536, "aff6a157e5ff3ee638798b72b13c1d906a99566c"),
        (65537, "4b745963cc290677dffc6a1fc86e714230ad1093"),
        (100000, "020b6c3c383316a5d9956b0f1151182c98b086bf"),
        (131072, "33c4cdfc4665490d74cbe8012874072018e428fb"),
        (131073, "4ff117ef9ced35051329a5702c8aa04d4abffe7c"),
    ],
)
def test_hash_vectors_at_block_boundaries(tmp_path: Path, size: int, expected: str) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(bytes(i % 251 for i in range(size)))

    assert plex_hash.calculate_plex_hash(source) == expected


def test_reads_at_most_two_blocks_and_reuses_stable_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 1_000_000)
    reads: list[int] = []
    real_open = builtins.open

    class RecordingReader:
        def __init__(self, filename: str, mode: str) -> None:
            self.file = real_open(filename, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, size: int):
            reads.append(size)
            return self.file.read(size)

        def seek(self, offset: int):
            return self.file.seek(offset)

    monkeypatch.setattr(plex_hash, "open", RecordingReader, raising=False)

    first = plex_hash.calculate_plex_hash(source)
    second = plex_hash.calculate_plex_hash(source)

    assert first == second
    assert reads == [65536, 65536]


@pytest.mark.parametrize("replace_inode", [False, True])
def test_same_size_replacement_with_preserved_mtime_invalidates_hash(tmp_path: Path, replace_inode: bool) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    previous = plex_hash.calculate_plex_hash(source)
    before = source.stat()
    target = tmp_path / "replacement.mkv" if replace_inode else source
    target.write_bytes(b"b" * 70000)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    if replace_inode:
        target.replace(source)

    assert plex_hash.calculate_plex_hash(source) != previous


def test_replacement_after_hashing_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    real_hash = plex_hash._hash_stable_source

    def replace_after_read(path, fingerprint):
        result = real_hash(path, fingerprint)
        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"b" * 70000)
        replacement.replace(source)
        return result

    monkeypatch.setattr(plex_hash, "_hash_stable_source", replace_after_read)
    with pytest.raises(plex_hash.SourceFileChangedError, match="changed while hashing"):
        plex_hash.calculate_plex_hash(source)


def test_modification_during_hashing_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    real_open = builtins.open

    class MutatingReader:
        def __init__(self, filename: str, mode: str) -> None:
            self.file = real_open(filename, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, size):
            result = self.file.read(size)
            with real_open(source, "ab") as writer:
                writer.write(b"replacement bytes")
            return result

        def seek(self, offset):
            return self.file.seek(offset)

    monkeypatch.setattr(plex_hash, "open", MutatingReader, raising=False)
    with pytest.raises(plex_hash.SourceFileChangedError, match="changed while hashing"):
        plex_hash.calculate_plex_hash(source)


def test_unreadable_source_propagates_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"video")

    def denied(*args, **kwargs):
        raise PermissionError("media mount unreadable")

    monkeypatch.setattr(plex_hash, "open", denied, raising=False)
    with pytest.raises(PermissionError, match="media mount unreadable"):
        plex_hash.calculate_plex_hash(source)


def test_non_file_source_rejected(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="not a regular file"):
        plex_hash.calculate_plex_hash(tmp_path)


def test_missing_source_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        plex_hash.calculate_plex_hash(tmp_path / "missing.mkv")


class _OtherCtime:
    """``os.fstat`` answering with another ``st_ctime``, as Windows' Python 3.12+ does (there it is the last change)."""

    def __init__(self, real_fstat, shift_ns: int) -> None:
        self.real_fstat, self.shift_ns = real_fstat, shift_ns

    def __call__(self, fd: int):
        info = self.real_fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        return type(
            "Stat", (), {**{f: getattr(info, f) for f in fields}, "st_ctime_ns": info.st_ctime_ns + self.shift_ns}
        )


@pytest.mark.parametrize("size", [5, 70000], ids=["small", "large"])
def test_an_opened_files_other_ctime_isnt_a_change(tmp_path, monkeypatch, size):
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * size)
    expected = plex_hash.calculate_plex_hash(source)
    plex_hash._hash_stable_source.cache_clear()
    monkeypatch.setattr(plex_hash.os, "fstat", _OtherCtime(os.fstat, 1_000_000_000))

    assert plex_hash.calculate_plex_hash(source) == expected
    plex_hash._hash_stable_source.cache_clear()


def test_a_file_changed_while_hashing_is_rejected_whatever_its_ctime(tmp_path, monkeypatch):
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    plex_hash._hash_stable_source.cache_clear()
    real_fstat, calls = os.fstat, []

    def grows_after_opening(fd):
        calls.append(fd)
        if len(calls) == 2:
            with open(source, "ab") as writer:
                writer.write(b"more")
        return _OtherCtime(real_fstat, 1)(fd)

    monkeypatch.setattr(plex_hash.os, "fstat", grows_after_opening)

    with pytest.raises(plex_hash.SourceFileChangedError, match="changed while hashing"):
        plex_hash.calculate_plex_hash(source)


class TestSameSource:
    def test_true_when_only_the_change_time_differs(self):
        assert plex_hash.same_source((1, 2, 3, 4, 5), (1, 2, 3, 4, 6))

    def test_compares_a_saved_json_list_with_a_live_tuple(self):
        assert plex_hash.same_source([1, 2, 3, 4, 5], (1, 2, 3, 4, 9))
        assert plex_hash.same_source((1, 2, 3, 4, 5), [1, 2, 3, 4, 9])

    @pytest.mark.parametrize("field", range(4), ids=["device", "inode", "size", "mtime"])
    def test_false_when_device_inode_size_or_mtime_differs(self, field):
        changed = [1, 2, 3, 4, 5]
        changed[field] += 1
        assert not plex_hash.same_source((1, 2, 3, 4, 5), changed)

    @pytest.mark.parametrize("saved", [None, [1, 2, 3], [1, 2, 3, 4], {"a": 1}, "12345", 7], ids=str)
    def test_false_for_missing_or_malformed_saved_values(self, saved):
        assert not plex_hash.same_source(saved, (1, 2, 3, 4, 5))
        assert not plex_hash.same_source((1, 2, 3, 4, 5), saved)


class TestSameRecordedSource:
    @pytest.mark.parametrize("field", [0, 1, 4], ids=["device", "inode", "ctime"])
    def test_true_when_only_device_inode_or_change_time_differ(self, field):
        changed = [1, 2, 3, 4, 5]
        changed[field] += 1
        assert plex_hash.same_recorded_source(changed, (1, 2, 3, 4, 5))

    @pytest.mark.parametrize("field", [2, 3], ids=["size", "mtime"])
    def test_false_when_size_or_mtime_differs(self, field):
        changed = [1, 2, 3, 4, 5]
        changed[field] += 1
        assert not plex_hash.same_recorded_source(changed, (1, 2, 3, 4, 5))

    @pytest.mark.parametrize("saved", [None, [1, 2, 3], [1, 2, 3, 4], {"a": 1}, "12345", 7], ids=str)
    def test_false_for_missing_or_malformed_saved_values(self, saved):
        assert not plex_hash.same_recorded_source(saved, (1, 2, 3, 4, 5))
        assert not plex_hash.same_recorded_source((1, 2, 3, 4, 5), saved)


@pytest.mark.parametrize("how", CTIME_ONLY_CHANGES)
def test_a_metadata_only_change_keeps_the_fingerprint_and_hash(tmp_path, how):
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    plex_hash._hash_stable_source.cache_clear()
    before = plex_hash.get_source_fingerprint(source)

    change_ctime_only(source, how)

    assert plex_hash.same_source(before, plex_hash.get_source_fingerprint(source))
    assert plex_hash.calculate_plex_hash(source, fingerprint=before) == plex_hash.calculate_plex_hash(source)


@pytest.mark.parametrize("how", CTIME_ONLY_CHANGES)
def test_a_metadata_only_change_while_hashing_isnt_rejected(tmp_path, monkeypatch, how):
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    plex_hash._hash_stable_source.cache_clear()
    real_hash = plex_hash._hash_stable_source

    def relinked_after_read(path, fingerprint):
        result = real_hash(path, fingerprint)
        change_ctime_only(source, how)
        return result

    monkeypatch.setattr(plex_hash, "_hash_stable_source", relinked_after_read)

    assert plex_hash.calculate_plex_hash(source)


@pytest.mark.parametrize("how", SOURCE_CHANGES)
def test_a_real_change_still_invalidates_the_fingerprint(tmp_path, how):
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    plex_hash._hash_stable_source.cache_clear()
    before = plex_hash.get_source_fingerprint(source)

    change_source(source, how)

    assert not plex_hash.same_source(before, plex_hash.get_source_fingerprint(source))
    with pytest.raises(plex_hash.SourceFileChangedError, match="changed before hashing"):
        plex_hash.calculate_plex_hash(source, fingerprint=before)
