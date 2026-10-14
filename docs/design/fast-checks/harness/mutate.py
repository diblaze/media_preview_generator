#!/usr/bin/env python3
"""Make the `mutated` snapshot from the `steady` one by changing STATE ONLY (never media).

Usage: mutate.py SNAP_DIR STEADY MUTATED [SEED]

Copies SNAP_DIR/STEADY to SNAP_DIR/MUTATED, then, for fixed seeded random picks of about 5% of the tv + movies files
(about 3% for "looks replaced"), makes the state look like a different life story:

  plex_markers_deleted    Plex's intro/credit rows (tag type 12) removed from the item
  evidence_forgotten      markers.db rows for the file (evidence, decisions, markers, publish state...) removed,
                          so the file looks never analysed
  looks_replaced          markers.db stored size and mtime changed, so the file looks like a new file
  bif_deleted             the item's index-sd.bif (+ .meta) removed from Media/localhost
  chapters_deleted        the item's chapter images removed (half also lose their manifest)
  loudness_streams_wiped  ln:* fields removed from the item's audio streams
  loudness_mark_removed   ln:loudnessAnalysisVersion removed from the item

Every pick is written to SNAP_DIR/MUTATED/mutations.json (kind -> list of file paths) so a run can be explained.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import sys
import urllib.parse

PLEX_DB = "com.plexapp.plugins.library.db"
PICKS = {
    "plex_markers_deleted": 0.05,
    "evidence_forgotten": 0.05,
    "looks_replaced": 0.03,
    "bif_deleted": 0.05,
    "chapters_deleted": 0.05,
    "loudness_streams_wiped": 0.05,
    "loudness_mark_removed": 0.05,
}
FILE_TABLES_BY_FILE_ID = [
    "evidence",
    "evidence_versions",
    "markers",
    "decisions",
    "publish_state",
    "publish_basis",
    "server_kinds",
    "detector_runs",
    "detector_failures",
    "intro_chapter_limits",
    "replaced_decisions",
    "replaced_versions",
    "version_reruns",
    "server_marker_rereads",
]


def decode(extra: str | None) -> dict:
    if not extra:
        return {}
    try:
        value = json.loads(extra)
    except ValueError:
        return dict(urllib.parse.parse_qsl(extra, keep_blank_values=True))
    return value if isinstance(value, dict) else {}


def url_form(fields: dict) -> str:
    # Plex's own URL-encoding: sorted keys, '.' written as %2E.
    pairs = "&".join(
        f"{urllib.parse.quote(k, safe='-')}={urllib.parse.quote(str(v), safe='-')}".replace(".", "%2E")
        for k, v in sorted(fields.items())
    )
    return pairs


def encode(fields: dict) -> str:
    ordered = {k: fields[k] for k in sorted(fields) if k != "url"}
    ordered["url"] = url_form(ordered)
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def bundle_dir(root: str, part_hash: str) -> str:
    return os.path.join(root, part_hash[0], part_hash[1:] + ".bundle", "Contents")


def main() -> None:
    snap_dir, steady, mutated = sys.argv[1:4]
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 20261014
    src, dst = os.path.join(snap_dir, steady), os.path.join(snap_dir, mutated)
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)

    plex = sqlite3.connect(os.path.join(dst, "plex", "Databases", PLEX_DB))
    markers = sqlite3.connect(os.path.join(dst, "app", "markers.db"))
    markers.execute("PRAGMA foreign_keys=ON")
    localhost = os.path.join(dst, "plex", "localhost")

    rows = markers.execute(
        "SELECT id, canonical_path FROM files WHERE missing_since IS NULL "
        "AND (canonical_path LIKE '/media/tv/%' OR canonical_path LIKE '/media/movies/%') ORDER BY canonical_path"
    ).fetchall()
    rng = random.Random(seed)
    chosen: dict[str, list[tuple[int, str]]] = {}
    for kind, share in PICKS.items():
        chosen[kind] = sorted(rng.sample(rows, max(1, round(len(rows) * share))), key=lambda r: r[1])

    def plex_item(path: str):
        return plex.execute(
            "SELECT mp.id, mp.hash, mi.id, mi.metadata_item_id FROM media_parts mp "
            "JOIN media_items mi ON mi.id = mp.media_item_id WHERE mp.file = ? AND mp.deleted_at IS NULL",
            (path,),
        ).fetchall()

    log: dict[str, list[str]] = {kind: [] for kind in PICKS}
    for file_id, path in chosen["evidence_forgotten"]:
        for table in FILE_TABLES_BY_FILE_ID:
            markers.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))  # noqa: S608
        log["evidence_forgotten"].append(path)
    for file_id, path in chosen["looks_replaced"]:
        markers.execute(
            "UPDATE files SET size = size + 4096, mtime_ns = mtime_ns + 7000000000 WHERE id = ?", (file_id,)
        )
        log["looks_replaced"].append(path)

    for _file_id, path in chosen["plex_markers_deleted"]:
        for _part, _hash, _media, item in plex_item(path):
            plex.execute(
                "DELETE FROM taggings WHERE metadata_item_id = ? AND tag_id IN (SELECT id FROM tags WHERE tag_type = 12)",
                (item,),
            )
        log["plex_markers_deleted"].append(path)
    for _file_id, path in chosen["bif_deleted"]:
        for _part, part_hash, _media, _item in plex_item(path):
            indexes = os.path.join(bundle_dir(localhost, part_hash or "x0"), "Indexes")
            for name in ("index-sd.bif", "index-sd.bif.meta"):
                if os.path.exists(os.path.join(indexes, name)):
                    os.remove(os.path.join(indexes, name))
        log["bif_deleted"].append(path)
    for position, (_file_id, path) in enumerate(chosen["chapters_deleted"]):
        for _part, part_hash, _media, _item in plex_item(path):
            chapters = os.path.join(bundle_dir(localhost, part_hash or "x0"), "Chapters")
            if not os.path.isdir(chapters):
                continue
            for name in os.listdir(chapters):
                if name.endswith(".jpg") or (position % 2 == 0 and name == "chapter-thumbnails.mpg.json"):
                    os.remove(os.path.join(chapters, name))
        log["chapters_deleted"].append(path)
    for _file_id, path in chosen["loudness_streams_wiped"]:
        for part, _hash, _media, _item in plex_item(path):
            for stream_id, extra in plex.execute(
                "SELECT id, extra_data FROM media_streams WHERE media_part_id = ? AND stream_type_id = 2", (part,)
            ).fetchall():
                fields = decode(extra)
                kept = {k: v for k, v in fields.items() if not k.startswith("ln:") and k != "url"}
                if kept != {k: v for k, v in fields.items() if k != "url"}:
                    plex.execute(
                        "UPDATE media_streams SET extra_data = ? WHERE id = ?",
                        (encode(kept) if kept else None, stream_id),
                    )
        log["loudness_streams_wiped"].append(path)
    for _file_id, path in chosen["loudness_mark_removed"]:
        for _part, _hash, _media, item in plex_item(path):
            row = plex.execute("SELECT extra_data FROM metadata_items WHERE id = ?", (item,)).fetchone()
            fields = decode(row[0] if row else None)
            if "ln:loudnessAnalysisVersion" in fields:
                del fields["ln:loudnessAnalysisVersion"]
                plex.execute("UPDATE metadata_items SET extra_data = ? WHERE id = ?", (encode(fields), item))
        log["loudness_mark_removed"].append(path)

    plex.commit()
    plex.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    markers.commit()
    plex.close()
    markers.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    markers.close()
    with open(os.path.join(dst, "mutations.json"), "w") as handle:
        json.dump({"seed": seed, "files_considered": len(rows), "picks": log}, handle, indent=1)
    print({kind: len(paths) for kind, paths in log.items()}, "of", len(rows), "files", file=sys.stderr)


if __name__ == "__main__":
    main()
