#!/usr/bin/env python3
"""Small damage test: does the new code notice exactly the files whose outputs were taken away?

Usage (env FASTCHECK_APP_URL, FASTCHECK_TOKEN for the job steps):
  small.py run OUT_JSON            run previews, Intro & Credits and loudness on FILES; write per-file outcomes
  small.py damage PLEXDB MARKERSDB PLAN_JSON
                                   damage a copy of Plex's database and markers.db (app and Plex stopped); write the
                                   plan (which file got which damage, which BIFs to delete)
  small.py check BASELINE AFTER PLAN_JSON
                                   pass when every damaged file was redone and every other file stayed up to date
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))

TV = "/media/tv"
FILES_GLOBS = [
    f"{TV}/Community (2009) {{tvdb-94571}}/Season 04",
    f"{TV}/Severance (2022) {{tvdb-371980}}/Season 01",
    "/media/movies/Frozen Fever (2015) {tmdb-326359}",
    "/media/movies/Once Upon a Studio (2023) {tmdb-1139087}",
]
SERVER = "mlab-plex"
VIDEO = (".mkv", ".mp4", ".avi", ".m4v")
# Plex extras (trailers etc.) have no item of their own, so previews keep retrying them: not what this test is about.
EXTRAS = ("-trailer.", "-featurette.", "-behindthescenes.", "-deleted.", "-interview.", "-scene.", "-short.", "-other.")


def files_in(plex_db: str) -> list[str]:
    plex = sqlite3.connect(plex_db)
    paths = [
        row[0]
        for row in plex.execute("SELECT file FROM media_parts WHERE deleted_at IS NULL")
        if any(row[0].startswith(folder + "/") for folder in FILES_GLOBS)
        and row[0].endswith(VIDEO)
        and not any(extra in row[0] for extra in EXTRAS)
    ]
    plex.close()
    return sorted(paths)


def run(out: str, files: list[str]) -> None:
    import run_jobs

    run_jobs.wait_healthy()
    results = {}
    for kind, path, body in (
        ("previews", "/api/jobs/manual", {"file_paths": files, "priority": 1}),
        ("intro_credits", "/api/markers/jobs", {"file_paths": files, "priority": "high"}),
        ("loudness", "/api/loudness/jobs", {"file_paths": files, "priority": "high"}),
    ):
        created = run_jobs.call("POST", path, body)
        ids = created.get("jobs") or [created.get("id") or created.get("job_id")]
        for job_id in ids:
            run_jobs.wait_job(job_id)
        run_jobs.settle()
        per_file = {}
        for job_id in ids:
            log = os.path.join(os.environ["FASTCHECK_CFG"], "logs", "job_file_results", f"{job_id}.jsonl")
            if os.path.exists(log):
                for line in open(log):
                    row = json.loads(line)
                    per_file[row["file"]] = {
                        "outcome": row["outcome"],
                        "reason": row.get("reason", ""),
                        "servers": [
                            {k: s.get(k) for k in ("status", "frame_source", "artifacts")}
                            for s in row.get("servers", [])
                        ],
                    }
        results[kind] = per_file
        print(kind, {f: r["outcome"] for f, r in per_file.items()}, file=sys.stderr, flush=True)
    with open(out, "w") as handle:
        json.dump(results, handle, indent=1)


def damage(plex_db: str, markers_db: str, plan_out: str) -> None:
    from mutate import decode, encode

    files = files_in(plex_db)
    tv = [f for f in files if f.startswith(TV)]
    movies = [f for f in files if not f.startswith(TV)]
    # Each damage gets its own files so a redo can be traced to one cause.
    plan = {
        "bif_deleted": [tv[0], movies[0]],
        "plex_markers_deleted": [tv[1], tv[9]],
        "loudness_streams_wiped": [tv[2], movies[1]],
        "loudness_mark_removed": [tv[3], tv[10]],
        "evidence_forgotten": [tv[4]],
        "looks_replaced": [tv[5]],
        "chapters_deleted": [tv[6], tv[11]],
    }
    plex = sqlite3.connect(plex_db)
    markers = sqlite3.connect(markers_db)
    markers.execute("PRAGMA foreign_keys=ON")

    def parts(path):
        return plex.execute(
            "SELECT mp.id, mp.hash, mi.metadata_item_id FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id "
            "WHERE mp.file = ? AND mp.deleted_at IS NULL",
            (path,),
        ).fetchall()

    bundles = {"bif": [], "chapters": []}
    for path in plan["bif_deleted"]:
        bundles["bif"] += [h for _p, h, _i in parts(path)]
    for path in plan["chapters_deleted"]:
        bundles["chapters"] += [h for _p, h, _i in parts(path)]
    for path in plan["plex_markers_deleted"]:
        for _p, _h, item in parts(path):
            plex.execute(
                "DELETE FROM taggings WHERE metadata_item_id = ? AND tag_id IN (SELECT id FROM tags WHERE tag_type = 12)",
                (item,),
            )
    for path in plan["loudness_streams_wiped"]:
        for part, _h, _i in parts(path):
            for stream_id, extra in plex.execute(
                "SELECT id, extra_data FROM media_streams WHERE media_part_id = ? AND stream_type_id = 2", (part,)
            ).fetchall():
                kept = {k: v for k, v in decode(extra).items() if not k.startswith("ln:") and k != "url"}
                plex.execute(
                    "UPDATE media_streams SET extra_data = ? WHERE id = ?", (encode(kept) if kept else None, stream_id)
                )
    for path in plan["loudness_mark_removed"]:
        for _p, _h, item in parts(path):
            fields = decode(plex.execute("SELECT extra_data FROM metadata_items WHERE id = ?", (item,)).fetchone()[0])
            fields.pop("ln:loudnessAnalysisVersion", None)
            plex.execute("UPDATE metadata_items SET extra_data = ? WHERE id = ?", (encode(fields), item))
    from mutate import FILE_TABLES_BY_FILE_ID

    for path in plan["evidence_forgotten"]:
        (file_id,) = markers.execute("SELECT id FROM files WHERE canonical_path = ?", (path,)).fetchone()
        for table in FILE_TABLES_BY_FILE_ID:
            markers.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))  # noqa: S608
    for path in plan["looks_replaced"]:
        markers.execute(
            "UPDATE files SET size = size + 4096, mtime_ns = mtime_ns + 7000000000 WHERE canonical_path = ?", (path,)
        )
    plex.commit()
    plex.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    plex.close()
    markers.commit()
    markers.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    markers.close()
    with open(plan_out, "w") as handle:
        json.dump({"files": files, "plan": plan, "bundles": bundles}, handle, indent=1)


# What a damaged file must show in the run after the damage; every other file must be up to date in every job type.
EXPECT = {
    "bif_deleted": ("previews", {"generated"}),
    "plex_markers_deleted": ("intro_credits", {"markers_published"}),
    "loudness_streams_wiped": ("loudness", {"loudness_written"}),
    # The streams still hold their values, so the item is only marked analysed again (no new streams to write).
    "loudness_mark_removed": ("loudness", {"loudness_up_to_date"}),
    # The BIF is kept and the chapter images are made again, which counts as generated.
    "chapters_deleted": ("previews", {"generated"}),
}
# What the reason must say for those, so an "up to date" that did nothing can't pass.
EXPECT_REASON = {"loudness_mark_removed": "item marked analysed"}
UP_TO_DATE = {
    "previews": {"skipped_bif_exists"},
    "intro_credits": {"markers_up_to_date", "markers_none"},
    "loudness": {"loudness_up_to_date"},
}


def check(baseline_path: str, after_path: str, plan_path: str) -> int:
    baseline, after = json.load(open(baseline_path)), json.load(open(after_path))
    plan = json.load(open(plan_path))
    files, damaged = plan["files"], plan["plan"]
    problems = []
    for kind, ok in UP_TO_DATE.items():
        for path in files:
            got = baseline[kind].get(path, {}).get("outcome")
            if got not in ok:
                problems.append(f"baseline {kind}: {got} for {path}")
    expected = {}
    for damage_kind, paths in damaged.items():
        for path in paths:
            if damage_kind in EXPECT:
                kind, outcomes = EXPECT[damage_kind]
                expected[(kind, path)] = outcomes
    for kind, ok in UP_TO_DATE.items():
        for path in files:
            got = after[kind].get(path, {}).get("outcome")
            want = expected.get((kind, path), ok)
            if got not in want:
                problems.append(f"after {kind}: {got} (want {sorted(want)}) for {path}")
    for damage_kind, words in EXPECT_REASON.items():
        kind = EXPECT[damage_kind][0]
        for path in damaged[damage_kind]:
            reason = after[kind].get(path, {}).get("reason", "")
            if words not in reason:
                problems.append(f"after {kind}: reason {reason!r} lacks {words!r} for {path}")
    for path in damaged["chapters_deleted"]:
        servers = after["previews"].get(path, {}).get("servers") or [{}]
        chapters = (servers[0].get("artifacts") or {}).get("chapters") or {}
        if not chapters.get("updated") or chapters.get("completed") != chapters.get("total"):
            problems.append(f"chapters not redone ({chapters}) for {path}")
    print(json.dumps({"files": len(files), "damaged": {k: len(v) for k, v in damaged.items()}}))
    for line in problems:
        print("PROBLEM", line)
    print("PASS" if not problems else f"FAIL ({len(problems)})")
    return 1 if problems else 0


if __name__ == "__main__":
    step = sys.argv[1]
    if step == "run":
        run(
            sys.argv[2],
            json.load(open(sys.argv[3]))["files"] if len(sys.argv) > 3 else files_in(os.environ["FASTCHECK_PLEXDB"]),
        )
    elif step == "damage":
        damage(sys.argv[2], sys.argv[3], sys.argv[4])
    elif step == "check":
        sys.exit(check(sys.argv[2], sys.argv[3], sys.argv[4]))
