"""Throwaway prototype of the master side: runs each op locally AND on the node, compares results byte for byte.

Usage (inside the app image, media at the master's path): python proto_master.py <master_path> <node_url> <gpu|cpu>
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

import requests

sys.path.insert(0, os.path.dirname(__file__))
import proto_node  # noqa: E402  (reuses the same op functions for the local baseline)

PATH, NODE, MODE = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3]
KEY = os.environ["NODE_KEY"]
H = {"Authorization": f"Bearer {KEY}"}
MAPPING = {"/data": "/mnt/nas"}
st = os.stat(PATH)
EXPECT = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
FFMPEG = "/usr/local/bin/ffmpeg"
REPORT: dict[str, object] = {"file": PATH, "mode": MODE}


def say(k: str, v: object) -> None:
    REPORT[k] = v
    print(f"{k}: {json.dumps(v)[:600]}", flush=True)


def remote(op: str, params: dict, mapping: dict | None = None, expect: dict | None = EXPECT, poll: float = 0.5) -> dict:
    spec = {"op": op, "path": PATH, "mapping": mapping or MAPPING, "expect": expect, "params": params}
    tid = requests.post(f"{NODE}/v1/tasks", json=spec, headers=H, timeout=10).json()["id"]
    polls = 0
    while True:
        s = requests.get(f"{NODE}/v1/tasks/{tid}", headers=H, timeout=10).json()
        polls += 1
        if s["status"] != "running":
            s["id"], s["polls"] = tid, polls
            return s
        time.sleep(poll)


def local(op: str, params: dict) -> dict:
    task = {"cancel": threading.Event(), "log": []}
    t0 = time.monotonic()
    res = proto_node.OPS[op](task, PATH, params)
    return {"result": res, "frames_dir": task.get("frames_dir"), "seconds": round(time.monotonic() - t0, 2)}


def digest_dir(d: str) -> dict[str, str]:
    return {f: hashlib.sha256(open(os.path.join(d, f), "rb").read()).hexdigest() for f in sorted(os.listdir(d))}


def bif_of(d: str) -> str:
    from media_preview_generator.processing.generator import generate_bif

    out = os.path.join(tempfile.mkdtemp(), "index-sd.bif")
    generate_bif(out, d, proto_node.ShimConfig(plex_bif_frame_interval=5, server_display_name=None))
    return hashlib.sha256(open(out, "rb").read()).hexdigest()


def probe() -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", PATH],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def main() -> None:
    gpu = gpu_dev = None
    info = requests.get(f"{NODE}/v1/info", headers=H, timeout=120).json()
    say("node_gpus", [(g["type"], g["device"], g["info"].get("name"), g["info"].get("status")) for g in info["gpus"]])
    say("node_gpu_detect_s", info["detect_s"])
    if MODE == "gpu":
        g = next(g for g in info["gpus"] if g["type"] == "NVIDIA")
        gpu, gpu_dev = g["type"], g["device"]

    # Access checks: good, unmapped, wrong file, traversal.
    checks = requests.post(
        f"{NODE}/v1/access-check",
        headers=H,
        json={
            "mapping": MAPPING,
            "files": [
                {"path": PATH, "expect": EXPECT},
                {"path": PATH.replace("/data", "/elsewhere", 1), "expect": EXPECT},
                {"path": PATH, "expect": {"size": EXPECT["size"] + 1, "mtime_ns": EXPECT["mtime_ns"]}},
                {"path": "/data/../etc/passwd", "expect": None},
                {"path": "/data/TV Shows/does-not-exist.mkv", "expect": None},
            ],
        },
        timeout=30,
    ).json()
    say("access_checks", [(c["ok"], c.get("why", c.get("node_path"))) for c in checks])
    bad_map = remote("loudness.analyze", {"tracks": []}, mapping={"/data": "/mnt/wrong"})
    say("task_with_wrong_mapping", {k: bad_map.get(k) for k in ("status", "error", "seconds")})

    meta = probe()
    dur_ms = int(float(meta["format"]["duration"]) * 1000)
    audio = next(s for s in meta["streams"] if s["codec_type"] == "audio")

    # preview.frames
    cfg = {
        "plex_bif_frame_interval": 5,
        "tonemap_algorithm": "hable",
        "server_display_name": None,
        "ffmpeg_path": FFMPEG,
        "ffmpeg_threads": 2,
        "log_level": "INFO",
        "thumbnail_quality": 4,
    }
    pp = {"config": cfg, "gpu": gpu, "gpu_device": gpu_dev}
    if not os.environ.get("SKIP_PREVIEW"):
        loc = local("preview.frames", pp)
        rem = remote("preview.frames", pp)
        t0 = time.monotonic()
        tar_bytes = requests.get(f"{NODE}/v1/tasks/{rem['id']}/frames.tar", headers=H, timeout=300).content
        fetch_s = round(time.monotonic() - t0, 2)
        rdir = tempfile.mkdtemp()
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            tar.extractall(rdir, filter="data")
        a, b = digest_dir(loc["frames_dir"]), digest_dir(rdir)
        say(
            "preview",
            {
                "local": loc["result"],
                "local_s": loc["seconds"],
                "remote": rem.get("result"),
                "remote_status": rem["status"],
                "remote_s": rem.get("seconds"),
                "polls": rem["polls"],
                "progress_seen": rem.get("progress"),
                "tar_mb": round(len(tar_bytes) / 1e6, 1),
                "fetch_s": fetch_s,
                "same_frame_names": list(a) == list(b),
                "frames_identical": a == b,
                "bif_identical": bif_of(loc["frames_dir"]) == bif_of(rdir),
                "error": rem.get("error"),
            },
        )

    # loudness.analyze
    lp = {"tracks": [[int(audio["index"]), audio.get("codec_name", ""), dur_ms]]}
    lr, rr = local("loudness.analyze", lp), remote("loudness.analyze", lp)
    say(
        "loudness",
        {
            "identical": lr["result"]["result"] == (rr.get("result") or {}).get("result"),
            "local": lr["result"]["result"],
            "remote_status": rr["status"],
            "error": rr.get("error"),
        },
    )

    # intro.fingerprint
    fp = {"duration_ms": dur_ms}
    lf, rf = local("intro.fingerprint", fp), remote("intro.fingerprint", fp)
    say(
        "fingerprint",
        {
            "identical": lf["result"]["points_hex"] == (rf.get("result") or {}).get("points_hex"),
            "n": lf["result"]["n"],
            "kb": round(len(lf["result"]["points_hex"]) / 2 / 1024, 1),
            "remote_status": rf["status"],
            "error": rf.get("error"),
        },
    )

    # intro.end_picture
    ep = {"start_s": 0.0, "length_s": 30.0, "ffmpeg": FFMPEG, "gpu": gpu, "gpu_device": gpu_dev}
    le, re_ = local("intro.end_picture", ep), remote("intro.end_picture", ep)
    say(
        "end_picture",
        {
            "identical": le["result"]["frames"] == (re_.get("result") or {}).get("frames"),
            "frames": len(le["result"]["frames"]),
            "remote_status": re_["status"],
            "error": re_.get("error"),
        },
    )

    # credits.find
    cp = {"duration_ms": dur_ms, "is_episode": True, "ffmpeg": FFMPEG, "gpu": gpu, "gpu_device": gpu_dev}
    lc, rc = local("credits.find", cp), remote("credits.find", cp)
    say(
        "credits",
        {
            "identical": lc["result"]["result"] == (rc.get("result") or {}).get("result"),
            "local_s": lc["seconds"],
            "remote_s": rc.get("seconds"),
            "summary": {k: v for k, v in lc["result"]["result"].items() if not isinstance(v, list | dict)},
            "cpu_notes": lc["result"]["cpu_notes"],
            "remote_status": rc["status"],
            "error": rc.get("error"),
        },
    )

    # cancel mid-run
    spec = {"op": "preview.frames", "path": PATH, "mapping": MAPPING, "expect": EXPECT, "params": pp}
    tid = requests.post(f"{NODE}/v1/tasks", json=spec, headers=H, timeout=10).json()["id"]
    time.sleep(3)
    t0 = time.monotonic()
    requests.post(f"{NODE}/v1/tasks/{tid}/cancel", headers=H, timeout=10)
    while (s := requests.get(f"{NODE}/v1/tasks/{tid}", headers=H, timeout=10).json())["status"] == "running":
        time.sleep(0.2)
    say("cancel", {"status": s["status"], "stop_s": round(time.monotonic() - t0, 2), "error": s.get("error")})

    # wrong key
    say(
        "wrong_key_status",
        requests.get(f"{NODE}/v1/info", headers={"Authorization": "Bearer nope"}, timeout=10).status_code,
    )
    json.dump(REPORT, open(f"/out/report-{MODE}.json", "w"), indent=1, default=str)


if __name__ == "__main__":
    main()
