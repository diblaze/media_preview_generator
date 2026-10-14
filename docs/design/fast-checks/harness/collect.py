#!/usr/bin/env python3
"""Turn one finished run (an app config dir + a copy of Plex's database and Media/localhost) into comparable JSON.

Usage: collect.py RUN_DIR OUT_DIR

RUN_DIR holds cfg/ (the app's config: logs, markers.db), plex/ (Databases/ and localhost/) and jobs.json.
OUT_DIR gets: jobs.json, decisions.json, results.json, plex.json, files.json, markers_db.json.

Everything that differs between two runs of identical code for reasons that are not behaviour is stripped here
(see NORMALISATION in the README): timestamps, durations, job ids, worker names, "saved <date>" / "asked now",
progress lines, and database columns holding times. Nothing here prints or stores a token.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

PLEX_SECTIONS = (1, 2)
MARKER_TAG, CHAPTER_TAG = 12, 9

# ---------------------------------------------------------------------------------------------------------- logs
LINE_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \w+ - ")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SHORT_JOB = re.compile(r"\b(job|Job) [0-9a-f]{8}\b")
WORKER_PREFIX = re.compile(r"^Fast-check CPU · \d+ ")
WORKER_NAME = re.compile(r"\b(?:CPU|GPU) Worker \d+(?: \([^)]*\))?")
DURATION = re.compile(r"\b\d+(?:\.\d+)? ?(?:ms|s|sec|seconds|min)\b(?! of)")
SAVED = re.compile(r"[;,]? ?saved \d{4}-\d{2}-\d{2}")
ASKED_NOW = re.compile(r"[;,]? ?asked now")
EMPTY_PARENS = re.compile(r"\s*\(\s*\)")
NOISE = (
    re.compile(r"^\[(?:CPU|GPU)[^\]]*\]: "),  # live ffmpeg progress
    re.compile(r"^Dispatcher progress"),
    re.compile(r"^Created fresh worker pool|^Reusing existing worker pool|^Created global JobDispatcher"),
    re.compile(r"^Added \d+ (?:CPU|GPU) worker"),
    re.compile(r"^Querying|^Plex library .*: (?:requesting|received)|^Found \d+ item\(s\)"),
    re.compile(r"^\[[^\]]+\] (?:Connecting to Plex|Successfully connected)"),
)


def normalise_line(raw: str) -> str | None:
    line = LINE_PREFIX.sub("", raw.rstrip("\n"))
    if not line or any(pattern.search(line) for pattern in NOISE):
        return None
    line = UUID.sub("<id>", line)
    line = SHORT_JOB.sub(r"\1 <id>", line)
    line = WORKER_PREFIX.sub("<worker> ", line)
    line = WORKER_NAME.sub("<worker>", line)
    line = SAVED.sub("", line)
    line = ASKED_NOW.sub("", line)
    line = DURATION.sub("<t>", line)
    line = EMPTY_PARENS.sub("", line)
    return line.strip()


def line_key(line: str) -> str:
    if " · " in line:
        return line.split(" · ", 1)[0]
    head, sep, _ = line.partition(": ")
    return head if sep and len(head) < 150 else "(job)"


def read_job_logs(cfg: str, kind: str, ids: list[str]):
    decisions: dict[str, list[str]] = defaultdict(list)
    results: dict[str, list] = defaultdict(list)
    for job_id in ids:
        path = os.path.join(cfg, "logs", "jobs", f"{job_id}.log")
        if os.path.exists(path):
            with open(path, errors="replace") as handle:
                for raw in handle:
                    line = normalise_line(raw)
                    if line:
                        decisions[line_key(line)].append(line)
        path = os.path.join(cfg, "logs", "job_file_results", f"{job_id}.jsonl")
        if os.path.exists(path):
            with open(path) as handle:
                for raw in handle:
                    row = json.loads(raw)
                    row.pop("ts", None)
                    row.pop("worker", None)
                    for server in row.get("servers") or []:
                        server["message"] = normalise_line("[2000-01-01 00:00:00] INFO - " + server.get("message", ""))
                    row["reason"] = normalise_line("[2000-01-01 00:00:00] INFO - " + row.get("reason", ""))
                    results[row.pop("file")].append(row)
    # Lines of one file keep their order; two files sharing a title interleave arbitrarily, so each title's lines
    # are sorted. What each file said is compared, not the order in which workers happened to say it.
    return {key: sorted(lines) for key, lines in sorted(decisions.items())}, {
        key: sorted(rows, key=json.dumps) for key, rows in sorted(results.items())
    }


# ------------------------------------------------------------------------------------------------------ plex db
VOLATILE_KEY = re.compile(r"date|stamp|changedat|created|blur|bandwidth|tz|accuracy", re.IGNORECASE)


def decode_extra(value: str | None, keep: re.Pattern | None = None) -> dict:
    if not value:
        return {}
    try:
        fields = json.loads(value)
    except ValueError:
        fields = {}
    if not isinstance(fields, dict):
        return {}
    return {
        k: v
        for k, v in sorted(fields.items())
        if k != "url" and not VOLATILE_KEY.search(k) and (keep is None or keep.match(k))
    }


def read_plex(db_path: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: dict[str, dict] = {"parts": {}, "markers": {}, "chapters": {}, "loudness": {}}
    items = conn.execute(
        "SELECT mp.id part_id, mp.file, mp.hash, mp.size, mp.duration, mp.extra_data part_extra, "
        "mi.metadata_item_id item_id, md.extra_data item_extra "
        "FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id "
        "JOIN metadata_items md ON md.id = mi.metadata_item_id "
        f"WHERE mp.deleted_at IS NULL AND md.library_section_id IN ({','.join(map(str, PLEX_SECTIONS))}) "
        "ORDER BY mp.file, mp.id"
    ).fetchall()
    tag_rows = conn.execute(
        "SELECT g.metadata_item_id, t.tag_type, g.'index' idx, g.text, g.time_offset, g.end_time_offset, g.thumb_url, "
        "g.extra_data FROM taggings g JOIN tags t ON t.id = g.tag_id WHERE t.tag_type IN (?, ?)",
        (MARKER_TAG, CHAPTER_TAG),
    ).fetchall()
    by_item: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
    for row in tag_rows:
        by_item[row["metadata_item_id"]][row["tag_type"]].append(
            [
                row["idx"],
                row["text"],
                row["time_offset"],
                row["end_time_offset"],
                row["thumb_url"],
                decode_extra(row["extra_data"]),
            ]
        )
    for item in items:
        path = item["file"]
        out["parts"][path] = {
            "hash": item["hash"],
            "size": item["size"],
            "duration": item["duration"],
            "part_flags": decode_extra(item["part_extra"]),
        }
        out["markers"][path] = sorted(by_item[item["item_id"]][MARKER_TAG], key=json.dumps)
        out["chapters"][path] = sorted(by_item[item["item_id"]][CHAPTER_TAG], key=json.dumps)
        streams = conn.execute(
            'SELECT "index" idx, codec, extra_data FROM media_streams '
            'WHERE media_part_id = ? AND stream_type_id = 2 ORDER BY "index", id',
            (item["part_id"],),
        ).fetchall()
        wanted = re.compile(r"(?:ln|mi):")
        out["loudness"][path] = {
            "item_mark": decode_extra(item["item_extra"], re.compile(r"ln:")),
            "streams": [[s["idx"], s["codec"], decode_extra(s["extra_data"], wanted)] for s in streams],
        }
    conn.close()
    return out


# --------------------------------------------------------------------------------------------- media/localhost
def read_files(root: str) -> dict:
    out = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            full = os.path.join(folder, name)
            digest = hashlib.sha256()
            with open(full, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            out[os.path.relpath(full, root)] = [os.path.getsize(full), digest.hexdigest()]
    return dict(sorted(out.items()))


# ----------------------------------------------------------------------------------------------------- markers.db
def read_markers_db(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    paths = dict(conn.execute("SELECT id, canonical_path FROM files"))
    out: dict[str, dict[str, list]] = {}
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for table in tables:
        if table.startswith("sqlite_") or table == "meta":
            continue
        cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - names come from sqlite_master
        columns = [c[0] for c in cursor.description]
        grouped: dict[str, list] = defaultdict(list)
        for row in cursor.fetchall():
            record: dict = {}
            owner = "(no file)"
            for column, value in zip(columns, row, strict=True):
                if column.endswith("_at") or column == "day":
                    continue
                if column == "id" and table == "files":
                    continue
                if column == "id" and table == "evidence":
                    continue
                if column in ("file_id", "file_a", "file_b"):
                    value = paths.get(value, f"(unknown file {value})")
                    if column in ("file_id", "file_a"):
                        owner = value
                if column == "missing_since":
                    value = value is not None
                if column == "canonical_path":
                    owner = value
                if isinstance(value, bytes):
                    value = hashlib.sha1(value, usedforsecurity=False).hexdigest()
                record[column] = value
            grouped[owner].append(record)
        out[table] = {k: sorted(v, key=lambda r: json.dumps(r, sort_keys=True)) for k, v in sorted(grouped.items())}
    conn.close()
    return out


def main() -> None:
    run_dir, out_dir = sys.argv[1:3]
    os.makedirs(out_dir, exist_ok=True)
    cfg = os.path.join(run_dir, "cfg")
    with open(os.path.join(run_dir, "jobs.json")) as handle:
        jobs = json.load(handle)
    decisions, results = {}, {}
    for job in jobs:
        decisions[job["name"]], results[job["name"]] = read_job_logs(cfg, job["name"], job["ids"])

    def dump(name: str, value: object) -> None:
        with open(os.path.join(out_dir, name), "w") as handle:
            json.dump(value, handle, indent=1, sort_keys=True)

    dump("jobs.json", jobs)
    dump("decisions.json", decisions)
    dump("results.json", results)
    dump("plex.json", read_plex(os.path.join(run_dir, "plex", "Databases", "com.plexapp.plugins.library.db")))
    dump("files.json", read_files(os.path.join(run_dir, "plex", "localhost")))
    dump("markers_db.json", read_markers_db(os.path.join(cfg, "markers.db")))


if __name__ == "__main__":
    main()
