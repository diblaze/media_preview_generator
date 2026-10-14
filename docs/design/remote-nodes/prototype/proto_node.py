"""Throwaway prototype of a worker node: runs the app's own heavy steps on request.

Start: python proto_node.py  (inside the app image, media mounted at its own path, NODE_KEY set)
Every task carries the master's path + the node mapping; the node maps, guards the roots, checks size+mtime_ns,
then runs the op in a thread with progress/log/cancel, exactly the shape the design proposes.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import threading
import time
import traceback
import uuid
from dataclasses import asdict, is_dataclass

import numpy as np
from flask import Flask, abort, jsonify, request, send_file

app = Flask(__name__)
KEY = os.environ["NODE_KEY"]
TASKS: dict[str, dict] = {}


@app.before_request
def _auth() -> None:
    if request.headers.get("Authorization") != f"Bearer {KEY}":
        abort(401)


def _map(master_path: str, mapping: dict[str, str]) -> str:
    for master_prefix, node_prefix in mapping.items():
        if master_path == master_prefix or master_path.startswith(master_prefix.rstrip("/") + "/"):
            return node_prefix.rstrip("/") + master_path[len(master_prefix.rstrip("/")) :]
    raise LookupError(f"no node mapping covers {master_path}")


def _resolve(spec: dict) -> str:
    """Map, refuse anything outside the mapped roots, and compare size + mtime_ns with the master's stat."""
    node_path = os.path.realpath(_map(spec["path"], spec["mapping"]))
    roots = [os.path.realpath(p) for p in spec["mapping"].values()]
    if not any(node_path == r or node_path.startswith(r + os.sep) for r in roots):
        raise PermissionError(f"{node_path} is outside the mapped roots")
    try:
        st = os.stat(node_path)
    except FileNotFoundError:
        raise FileNotFoundError(f"node can't see {node_path}") from None
    want = spec.get("expect")
    if want and (st.st_size != want["size"] or st.st_mtime_ns != want["mtime_ns"]):
        raise FileExistsError(
            f"different file at {node_path}: size {st.st_size} vs {want['size']}, "
            f"mtime_ns {st.st_mtime_ns} vs {want['mtime_ns']}"
        )
    return node_path


class ShimConfig:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


def _jsonable(x: object) -> object:
    if is_dataclass(x):
        return _jsonable(asdict(x))
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, BaseException):
        return {"error": type(x).__name__, "message": str(x)}
    return x


def _op_preview(task: dict, path: str, p: dict) -> dict:
    from media_preview_generator.processing.generator import generate_images

    out = tempfile.mkdtemp(prefix="frames-")
    cfg = ShimConfig(**p["config"])
    res = generate_images(
        path,
        out,
        p.get("gpu"),
        p.get("gpu_device"),
        cfg,
        progress_callback=lambda pct, *a, **k: task.update(progress=round(float(pct), 1)),
        cancel_check=task["cancel"].is_set,
        pause_check=lambda: False,
    )
    task["frames_dir"] = out
    return {"generate_images": list(res), "frames": len([f for f in os.listdir(out) if f.endswith(".jpg")])}


def _op_loudness(task: dict, path: str, p: dict) -> dict:
    from media_preview_generator.loudness import analyze

    ffmpeg = analyze.resolve_ffmpeg("ffmpeg")
    tracks = [tuple(t) for t in p["tracks"]]
    if len(tracks) == 1:
        index, codec, duration_ms = tracks[0]
        res = analyze.run(ffmpeg, path, index, duration_ms=duration_ms, codec=codec, cancel_check=task["cancel"].is_set)
    else:
        res = analyze.run_many(ffmpeg, path, tracks, cancel_check=task["cancel"].is_set)
    return {"ffmpeg": ffmpeg, "result": _jsonable(res)}


def _op_fingerprint(task: dict, path: str, p: dict) -> dict:
    from media_preview_generator.markers.audio.fingerprint import chromaprint_ffmpeg, compute_fingerprint

    pts = compute_fingerprint(
        path, p["duration_ms"], ffmpeg=chromaprint_ffmpeg(None), cancel_check=task["cancel"].is_set
    )
    return {"points_hex": pts.astype("<u4").tobytes().hex(), "n": int(pts.size)}


def _op_end_picture(task: dict, path: str, p: dict) -> dict:
    from media_preview_generator.markers.audio.end_picture import decode_frames

    frames = decode_frames(
        path,
        p["start_s"],
        p["length_s"],
        ffmpeg=p["ffmpeg"],
        gpu=p.get("gpu"),
        gpu_device_path=p.get("gpu_device"),
        container_start_s=p.get("container_start_s", 0.0),
        download_format=p.get("download_format"),
        cancel_check=task["cancel"].is_set,
    )
    return {"frames": [[t, a.astype("<f4").tobytes().hex(), list(a.shape)] for t, a in frames]}


def _op_credits(task: dict, path: str, p: dict) -> dict:
    from media_preview_generator.markers.credits.detector import find_credits
    from media_preview_generator.markers.credits.textdet_helper import get_textdet_pool

    pool = get_textdet_pool()
    gpu, dev = p.get("gpu"), p.get("gpu_device")
    notes: list[str] = []
    kw = dict(
        gpu=gpu,
        gpu_device_path=dev,
        gpu_worker=gpu is not None,
        on_cpu=notes.append,
        cancel_check=task["cancel"].is_set,
    )
    res = find_credits(
        path,
        duration_ms=p["duration_ms"],
        is_episode=p["is_episode"],
        tail_s=p.get("tail_s"),
        ffmpeg=p["ffmpeg"],
        detect_boxes=lambda planes: pool.detect_boxes(planes, **kw),
        read_text=lambda planes: pool.read_text(planes, **kw),
        gpu=gpu,
        gpu_device_path=dev,
        cancel_check=task["cancel"].is_set,
        earliest_start_s=p.get("earliest_start_s"),
    )
    return {"result": _jsonable(res), "cpu_notes": notes}


OPS = {
    "preview.frames": _op_preview,
    "loudness.analyze": _op_loudness,
    "intro.fingerprint": _op_fingerprint,
    "intro.end_picture": _op_end_picture,
    "credits.find": _op_credits,
}


def _run(task: dict, spec: dict) -> None:
    t0 = time.monotonic()
    try:
        path = _resolve(spec)
        task["log"].append(f"mapped to {path}")
        task["result"] = OPS[spec["op"]](task, path, spec.get("params", {}))
        task["status"] = "cancelled" if task["cancel"].is_set() else "done"
    except (LookupError, PermissionError, FileNotFoundError, FileExistsError) as e:
        task.update(status="file_not_visible", error=f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        task.update(status="failed", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-1500:])
    task["seconds"] = round(time.monotonic() - t0, 2)


@app.get("/v1/info")
def info() -> object:
    from media_preview_generator.gpu.detect import detect_all_gpus

    t0 = time.monotonic()
    gpus = detect_all_gpus()
    body = {
        "gpus": [{"type": t, "device": d, "info": i} for t, d, i in gpus],
        "detect_s": round(time.monotonic() - t0, 2),
    }
    json.dumps(body)  # proves the record is JSON-safe
    return jsonify(body)


@app.post("/v1/access-check")
def access_check() -> object:
    out = []
    for f in request.json["files"]:
        try:
            out.append(
                {"path": f["path"], "ok": True, "node_path": _resolve({**f, "mapping": request.json["mapping"]})}
            )
        except Exception as e:  # noqa: BLE001
            out.append({"path": f["path"], "ok": False, "why": f"{type(e).__name__}: {e}"})
    return jsonify(out)


@app.post("/v1/tasks")
def start() -> object:
    spec = request.json
    tid = uuid.uuid4().hex[:8]
    task = {"status": "running", "progress": 0.0, "log": [], "cancel": threading.Event()}
    TASKS[tid] = task
    threading.Thread(target=_run, args=(task, spec), daemon=True).start()
    return jsonify(id=tid)


@app.get("/v1/tasks/<tid>")
def status(tid: str) -> object:
    t = TASKS[tid]
    return jsonify({k: v for k, v in t.items() if k not in ("cancel", "frames_dir")})


@app.post("/v1/tasks/<tid>/cancel")
def cancel(tid: str) -> object:
    TASKS[tid]["cancel"].set()
    return jsonify(ok=True)


@app.get("/v1/tasks/<tid>/frames.tar")
def frames(tid: str) -> object:
    d = TASKS[tid]["frames_dir"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in sorted(os.listdir(d)):
            tar.add(os.path.join(d, name), arcname=name)
    buf.seek(0)
    return send_file(buf, mimetype="application/x-tar")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)  # nosec B104 - throwaway prototype inside a private docker network
