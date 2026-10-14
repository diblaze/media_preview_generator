"""What the loudness check reads per file when everything is already analysed, marked and verified.

The counts are upper bounds: a change that brings back a per-file read of the share, Plex's database, loudness.db or
Plex's API fails here.
"""

from __future__ import annotations

import builtins
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree.ElementTree import Element

import pytest

from media_preview_generator.loudness import job, undecodable
from media_preview_generator.loudness.analyze import ANALYSIS_VERSION
from media_preview_generator.loudness.guard import create_loudness_db
from media_preview_generator.loudness.plex_db import VERSION_FIELD
from media_preview_generator.markers.publishers.plex_db import encode_extra_data, plex_db_path
from media_preview_generator.output import plex_hash
from media_preview_generator.output.plex_hash import calculate_plex_hash
from media_preview_generator.processing.types import ProcessableItem

from .test_job import _cfg
from .test_plex_db import FIELDS, FIX, PLEX_ITEM_TRIGGERS

FILES = 10
PLEX_DIR = "/data/kids"


class Reads:
    """Counts of each slow read made while checking files."""

    def __init__(self, media_dir: Path) -> None:
        self.media_dir = str(media_dir)
        self.counts: Counter[str] = Counter()

    def per_file(self) -> dict[str, float]:
        return {key: value / FILES for key, value in sorted(self.counts.items())}


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Ten analysed, marked files in a WAL Plex database, a guarded database for it and a Plex API that serves them."""
    folder = tmp_path / "Plex Media Server"
    db_file = Path(plex_db_path(str(folder)))
    db_file.parent.mkdir(parents=True)
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (folder / "Preferences.xml").write_text('<Preferences ProcessedMachineIdentifier="plex-a"/>')
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("BEGIN;\n" + (FIX / "plex_schema_1_43.sql").read_text() + PLEX_ITEM_TRIGGERS + "\nCOMMIT;")
    marked = encode_extra_data({VERSION_FIELD: ANALYSIS_VERSION})
    analysed = encode_extra_data(FIELDS)
    metadata: dict[str, Element] = {}
    paths = []
    for n in range(1, FILES + 1):
        path = media_dir / f"Episode {n}.mp4"
        path.write_bytes(b"x" * (70_000 + n))
        paths.append(str(path))
        plex_file = f"{PLEX_DIR}/Episode {n}.mp4"
        conn.execute(
            "INSERT INTO metadata_items (id, metadata_type, title, extra_data) VALUES (?, 4, 'Ep', ?)", (n, marked)
        )
        conn.execute("INSERT INTO media_items (id, metadata_item_id, duration) VALUES (?, ?, 1000)", (n, n))
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, hash, size) VALUES (?, ?, ?, ?, ?)",
            (n, n, plex_file, calculate_plex_hash(path), path.stat().st_size),
        )
        conn.execute(
            'INSERT INTO media_streams (id, media_part_id, media_item_id, stream_type_id, "index", codec, extra_data) '
            "VALUES (?, ?, ?, 2, 1, 'aac', ?)",
            (n, n, n, analysed),
        )
        container = Element("MediaContainer")
        part = Element("Part", id=str(n), file=plex_file)
        container.append(part)
        part.append(
            Element(
                "Stream",
                id=str(n),
                index="1",
                streamType="2",
                canNormalizeLoudness="1",
                loudnessAnalysisVersion=ANALYSIS_VERSION,
                **{key: FIELDS[f"ln:{key}"] for key in ("loudness", "peak", "lra", "threshold", "gainOffset")},
            )
        )
        metadata[f"/library/metadata/{n}"] = container
    conn.commit()
    conn.close()
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(undecodable, "ffmpeg_identity", lambda binary: "test-ffmpeg")

    reads = Reads(media_dir)
    server = MagicMock()

    def query(url: str) -> Element:
        if url == "/identity":
            reads.counts["plex http /identity"] += 1
            return Element("MediaContainer", machineIdentifier="plex-a", version="1.43.4.12345-abc123")
        reads.counts["plex http metadata"] += 1
        return metadata[url]

    server._connect.return_value.query.side_effect = query
    cfg = _cfg(paths[0])
    cfg.output = {"plex_config_folder": str(folder)}
    guarded = create_loudness_db(cfg, server)
    ctx = job.LoudnessContext(registry=MagicMock(), ffmpeg="ffmpeg")
    ctx._dbs[cfg.id], ctx._ready[cfg.id] = guarded, ("", time.monotonic())
    with patch.object(job, "owners", return_value=[cfg]):
        yield ctx, [ProcessableItem(canonical_path=p, server_id="", title="ep") for p in paths], reads


def _count(monkeypatch, reads: Reads) -> None:
    real_stat, real_fstat, real_open, real_connect = os.stat, os.fstat, builtins.open, sqlite3.connect

    def on_media(path) -> bool:
        return isinstance(path, str | os.PathLike) and os.fspath(path).startswith(reads.media_dir)

    def stat(path, *args, **kwargs):
        if on_media(path):
            reads.counts["share stat"] += 1
        return real_stat(path, *args, **kwargs)

    def fstat(fd):
        reads.counts["share fstat"] += 1
        return real_fstat(fd)

    def opened(path, *args, **kwargs):
        if on_media(path):
            reads.counts["share open"] += 1
        return real_open(path, *args, **kwargs)

    def connect(target, *args, **kwargs):
        conn = real_connect(target, *args, **kwargs)
        kind = "loudness.db" if str(target).endswith("loudness.db") else "plex db"
        reads.counts[f"{kind} connect"] += 1
        conn.set_trace_callback(lambda _sql: reads.counts.update({f"{kind} statement": 1}))
        return conn

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(plex_hash, "open", opened, raising=False)
    monkeypatch.setattr(sqlite3, "connect", connect)


def test_checking_done_files_makes_only_the_reads_it_needs(library, monkeypatch):
    ctx, items, reads = library
    plex_hash._hash_stable_source.cache_clear()  # a big library never gets cache hits
    _count(monkeypatch, reads)

    outcomes = [job.check_item(item, ctx=ctx) for item in items]

    assert [o.outcome_key for o in outcomes] == [job.UP_TO_DATE] * FILES
    counts = reads.counts
    # Before: share stat 6 (now 3), open 1, fstat 2; Plex db connect 1, statements 8; loudness.db connect 1, statements 2;
    # Plex API /identity 2, metadata 1 - all per file.
    assert counts["share stat"] <= 3 * FILES  # the first look, and a re-stat before and after Plex's API check
    assert counts["share open"] <= FILES  # one 64 KB head read each; a library scan makes none (below)
    assert counts["share fstat"] <= 2 * FILES
    assert counts["plex db connect"] <= FILES
    assert counts["plex db statement"] <= 2 * FILES + 8  # journal check + the file's rows; the schema check once
    assert counts["loudness.db connect"] == 1  # the can't-decode list, once for the job
    assert counts["plex http /identity"] <= 1  # the connected server's identity, proven once per TRUST_S
    assert counts["plex http metadata"] == FILES  # verify_streams stays per item


def test_library_scan_checks_do_not_open_the_file_when_sizes_match(library, monkeypatch):
    ctx, items, reads = library
    ctx.trust_server_hash = True
    plex_hash._hash_stable_source.cache_clear()
    _count(monkeypatch, reads)

    outcomes = [job.check_item(item, ctx=ctx) for item in items]

    assert [o.outcome_key for o in outcomes] == [job.UP_TO_DATE] * FILES
    assert reads.counts["share open"] == 0
    assert reads.counts["share fstat"] == 0


@pytest.mark.parametrize(
    "cfg, expected",
    [
        ({"source": "manual", "file_paths": ["/a"]}, False),
        ({"source": "plex_webhook"}, False),
        ({"source": "manual", "retry_attempt": 1}, False),
        ({"source": "manual"}, True),
        ({"source": "schedule"}, True),
        ({"source": "schedule", "retry_attempt": 1}, False),
        ({}, False),
    ],
    ids=["file_paths", "sender_source", "retry", "manual_scan", "scheduled_scan", "scheduled_retry", "no_source"],
)
def test_trusts_server_hash_per_condition(cfg, expected):
    assert job.trusts_server_hash(cfg) is expected


def test_webhook_sourced_checks_still_hash(library, monkeypatch):
    ctx, items, reads = library
    assert not ctx.trust_server_hash
    plex_hash._hash_stable_source.cache_clear()
    _count(monkeypatch, reads)

    job.check_item(items[0], ctx=ctx)

    assert reads.counts["share open"] == 1


def test_library_scan_size_mismatch_still_waits(library):
    ctx, items, _reads = library
    ctx.trust_server_hash = True
    Path(items[0].canonical_path).write_bytes(b"y" * 5)

    outcome = job.check_item(items[0], ctx=ctx)

    assert outcome.outcome_key == job.WAITING
    assert "Plex's indexed source differs from the file; retry after its scan" in outcome.message


def test_worker_hashes_before_writing_even_on_a_library_scan(library):
    ctx, items, _reads = library
    ctx.trust_server_hash = True
    with patch.object(job, "calculate_plex_hash", wraps=calculate_plex_hash) as hashed:
        job.process_item(items[0], ctx=ctx)
    assert hashed.called


def test_concurrent_checks_give_the_same_outcomes(library):
    ctx, items, _reads = library
    with ThreadPoolExecutor(8) as pool:
        outcomes = list(pool.map(lambda item: job.check_item(item, ctx=ctx), items * 4))
    assert {o.outcome_key for o in outcomes} == {job.UP_TO_DATE}
