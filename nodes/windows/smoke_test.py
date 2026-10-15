"""Smoke test of a built Windows Plex database node (``.github/workflows/windows-node.yml``).

Starts the exe against a throwaway Plex folder holding a WAL database, then asks it what the app asks: its health
(no key), ``/v1/ping`` with and without the key, and the database file checks while another process holds the database
open (the lock proof, through ``winplatform`` inside the frozen build) and after it closed. Standard library only, so
it runs with any Python.

Usage: python nodes/windows/smoke_test.py dist/PlexDbNode/PlexDbNode.exe
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

AGENT_PROTOCOL_HEADER = "X-Marker-Agent-Protocol"
HOLDER = (
    "import sqlite3, sys\n"
    "conn = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
    "conn.execute('SELECT COUNT(*) FROM t').fetchall()\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _call(url: str, *, key: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {AGENT_PROTOCOL_HEADER: "1", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 - always http://127.0.0.1
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _check(condition: bool, what: str) -> None:
    print(("ok   " if condition else "FAIL ") + what, flush=True)
    if not condition:
        raise SystemExit(1)


def _make_plex_folder(root: str) -> str:
    plex = os.path.join(root, "Plex Media Server")
    db_dir = os.path.join(plex, "Plug-in Support", "Databases")
    os.makedirs(db_dir)
    db = os.path.join(db_dir, "com.plexapp.plugins.library.db")
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.close()
    return plex


def main(exe: str) -> int:
    root = tempfile.mkdtemp(prefix="plexdbnode-")
    plex = _make_plex_folder(root)
    db = os.path.join(plex, "Plug-in Support", "Databases", "com.plexapp.plugins.library.db")
    node_dir = os.path.join(root, "node")
    port = _free_port()
    key = secrets.token_urlsafe(24)
    env = {**os.environ, "NODE_KEY": key, "PLEX_CONFIG_DIR": plex}
    node = subprocess.Popen([exe, "--port", str(port), "--config-dir", node_dir], env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        status, health = 0, {}
        while time.monotonic() < deadline and node.poll() is None:
            try:
                status, health = _call(f"{base}/v1/health")
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.5)
        _check(status == 200 and health.get("ok") is True, f"GET /v1/health answers ({status})")
        _check("plex_db" in (health.get("node") or {}).get("roles", []), "the node says it has the Plex database role")

        status, _ = _call(f"{base}/v1/ping")
        _check(status == 401, f"GET /v1/ping without the key is refused ({status})")
        status, ping = _call(f"{base}/v1/ping", key=key)
        _check(status == 200 and ping.get("ok") is True, f"GET /v1/ping with the key answers ({status})")
        _check((ping.get("result") or {}).get("db_present") is True, "it sees Plex's database")

        holder = subprocess.Popen([sys.executable, "-c", HOLDER, db], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            _check(holder.stdout.readline().strip() == b"ready", "another process holds the database open")
            status, held = _call(f"{base}/v1/checks/file", key=key, body={})
            report = (held.get("result") or {}).get("report") or {}
            _check(status == 200, f"POST /v1/checks/file answers ({status})")
            _check(report.get("details", {}).get("lock_holder") is True, "the node sees it held open")
            _check(
                report.get("state") == "ready", f"the file checks pass ({report.get('state')}: {report.get('message')})"
            )
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
        status, free = _call(f"{base}/v1/checks/file", key=key, body={})
        report = (free.get("result") or {}).get("report") or {}
        _check(report.get("details", {}).get("lock_holder") is False, "and sees nobody holding it once it closed")
    finally:
        node.kill()
        node.wait(timeout=10)
        log = os.path.join(node_dir, "logs", "node.log")
        if os.path.exists(log):
            print("--- node.log ---")
            with open(log, encoding="utf-8", errors="replace") as fh:
                print(fh.read().replace(key, "****"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
