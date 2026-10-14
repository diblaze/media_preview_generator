#!/usr/bin/env python3
"""Compare what two runs did and print every difference in plain English.

Usage: diff.py OUT_A OUT_B [--limit N]

OUT_A / OUT_B hold one folder per start state (steady, mutated), each written by collect.py. A "difference" is one
(state, kind of thing, file or subject) that is not identical between A and B. The last lines are
`N differences` and the timing table. Exit status 1 when N > 0. The full list goes to OUT_B/../diff-report.txt.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

LABELS = {
    "decisions": "job log",
    "results": "file outcome",
    "plex.parts": "Plex part data",
    "plex.markers": "Plex intro/credit markers",
    "plex.chapters": "Plex chapters",
    "plex.loudness": "Plex loudness",
    "files": "Plex bundle file",
    "markers_db": "markers.db",
    "jobs": "job result",
}


def load(folder: str, name: str):
    path = os.path.join(folder, name)
    if not os.path.exists(path):
        return None
    with open(path) as handle:
        return json.load(handle)


def short(value: object, width: int = 220) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text if len(text) <= width else text[: width - 1] + "…"


def name_of(path: str) -> str:
    return os.path.basename(path) if path.startswith("/") else path


def describe(a: object, b: object) -> str:
    if isinstance(a, list) and isinstance(b, list):
        count_a, count_b = Counter(map(json.dumps, a)), Counter(map(json.dumps, b))
        only_a = [k for k in count_a if count_a[k] > count_b.get(k, 0)]
        only_b = [k for k in count_b if count_b[k] > count_a.get(k, 0)]
        parts = []
        if only_a:
            parts.append("only before: " + "; ".join(short(json.loads(k), 160) for k in only_a[:3]))
        if only_b:
            parts.append("only after: " + "; ".join(short(json.loads(k), 160) for k in only_b[:3]))
        extra = len(only_a) + len(only_b) - min(len(only_a), 3) - min(len(only_b), 3)
        return " | ".join(parts) + (f" (+{extra} more)" if extra > 0 else "")
    return f"before: {short(a)} | after: {short(b)}"


def compare_maps(state: str, category: str, a: dict, b: dict, out: list, prefix: str = "") -> None:
    for key in sorted(set(a) | set(b)):
        if a.get(key) == b.get(key):
            continue
        subject = prefix + name_of(key)
        if key not in a:
            out.append((state, category, subject, f"only the new code has an entry: {short(b[key])}"))
        elif key not in b:
            out.append((state, category, subject, f"only the old code has an entry: {short(a[key])}"))
        else:
            out.append((state, category, subject, describe(a[key], b[key])))


def compare_state(state: str, dir_a: str, dir_b: str, out: list) -> None:
    jobs_a, jobs_b = load(dir_a, "jobs.json") or [], load(dir_b, "jobs.json") or []
    for ja, jb in zip(jobs_a, jobs_b, strict=False):
        for field in ("status", "error"):
            if ja[field] != jb[field]:
                out.append((state, "jobs", ja["name"], f"{field}: before {ja[field]} | after {jb[field]}"))
        pa = [
            {k: p[k] for k in ("total_items", "processed_items", "outcome", "marker_sources")} for p in ja["progress"]
        ]
        pb = [
            {k: p[k] for k in ("total_items", "processed_items", "outcome", "marker_sources")} for p in jb["progress"]
        ]
        if pa != pb:
            out.append((state, "jobs", ja["name"], f"counts: before {short(pa)} | after {short(pb)}"))
    for name in ("decisions", "results"):
        data_a, data_b = load(dir_a, f"{name}.json") or {}, load(dir_b, f"{name}.json") or {}
        for kind in sorted(set(data_a) | set(data_b)):
            compare_maps(state, name, data_a.get(kind, {}), data_b.get(kind, {}), out, prefix=f"{kind}: ")
    plex_a, plex_b = load(dir_a, "plex.json") or {}, load(dir_b, "plex.json") or {}
    for section in ("parts", "markers", "chapters", "loudness"):
        compare_maps(state, f"plex.{section}", plex_a.get(section, {}), plex_b.get(section, {}), out)
    compare_maps(state, "files", load(dir_a, "files.json") or {}, load(dir_b, "files.json") or {}, out)
    db_a, db_b = load(dir_a, "markers_db.json") or {}, load(dir_b, "markers_db.json") or {}
    for table in sorted(set(db_a) | set(db_b)):
        compare_maps(state, "markers_db", db_a.get(table, {}), db_b.get(table, {}), out, prefix=f"{table}: ")


def timing(dir_a: str, dir_b: str, states: list[str]) -> list[str]:
    lines = [
        f"{'state':8} {'job':14} {'files':>6} {'old s':>9} {'new s':>9} {'old s/file':>11} {'new s/file':>11} {'speed-up':>9}"
    ]

    def figures(job: dict) -> tuple[int, float]:
        files = sum(p["total_items"] or 0 for p in job["progress"])
        seconds = sum(s for s in job["job_s"] if s is not None) or job["wall_s"]
        return files, seconds

    for state in states:
        jobs_a = load(os.path.join(dir_a, state), "jobs.json") or []
        jobs_b = load(os.path.join(dir_b, state), "jobs.json") or []
        for ja, jb in zip(jobs_a, jobs_b, strict=False):
            (files_a, secs_a), (files_b, secs_b) = figures(ja), figures(jb)
            per_a = secs_a / files_a if files_a else 0.0
            per_b = secs_b / files_b if files_b else 0.0
            ratio = f"{secs_a / secs_b:.2f}x" if secs_b else "n/a"
            lines.append(
                f"{state:8} {ja['name']:14} {files_b:6d} {secs_a:9.1f} {secs_b:9.1f} {per_a:11.3f} {per_b:11.3f} {ratio:>9}"
            )
    return lines


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    limit = 60
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
        args = [a for a in args if a != str(limit)]
    out_a, out_b = args[:2]
    states = sorted(set(os.listdir(out_a)) & set(os.listdir(out_b)))
    differences: list[tuple[str, str, str, str]] = []
    for state in states:
        compare_state(state, os.path.join(out_a, state), os.path.join(out_b, state), differences)

    report = [
        f"[{state}] {LABELS.get(category, category)} · {subject}: {text}"
        for state, category, subject, text in differences
    ]
    report_path = os.path.join(os.path.dirname(os.path.abspath(out_b)), "diff-report.txt")
    with open(report_path, "w") as handle:
        handle.write("\n".join(report) + ("\n" if report else ""))
    for line in report[:limit]:
        print(line)
    if len(report) > limit:
        print(f"... {len(report) - limit} more in {report_path}")
    by_kind = Counter((state, LABELS.get(cat, cat)) for state, cat, _s, _t in differences)
    for (state, kind), count in sorted(by_kind.items()):
        print(f"  {state}: {count} in {kind}")
    print(f"{len(differences)} differences")
    print()
    print("\n".join(timing(out_a, out_b, states)))
    return 1 if differences else 0


if __name__ == "__main__":
    sys.exit(main())
