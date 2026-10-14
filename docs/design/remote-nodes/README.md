# Remote nodes — design and build spec

Status: **building** (owner approved building before reviewing, 2026-10-15; owner reviews the design page afterwards
and may ask for changes). Design page (with diagrams + UI mockups):
https://claude.ai/artifact/G5nBhYJVFBekqJMQ93NCHa

## 0. Start here

* A **node** is another machine that lends the app its GPU/CPU (**worker** role) or opens Plex's database where Plex
  runs (**Plex database** role). One node can hold both roles. The app stays the brain: queue, schedules, retries,
  tokens, mappings, every write. A node runs one heavy step it is handed and returns frames, numbers or timestamps.
* The Plex database role **is** today's Plex helper (`plex-marker-agent`), folded in. Its wire protocol (agent
  protocol 1, header `X-Marker-Agent-Protocol`) is unchanged so every helper already deployed keeps working.
* Evidence that the design works (prototype runs, Windows CI results, the requeue spike) is in
  [`evidence/`](evidence/) and the throwaway prototype in [`prototype/`](prototype/). Read §9 before trusting any
  number.
* Build order and lane ownership: §8. Each phase gets an Architecture Review (`.claude/agents/architecture-review.md`)
  because it touches workers/concurrency, settings schema, auth and paths.

## 1. Repository layout

Everything about nodes lives in one package (`media_preview_generator/nodes/`) and one packaging folder
(`nodes/`). Code that runs **on a node** is under `nodes/server/` and is never imported by the app's web process;
code that runs **in the app** sits beside it. `protocol.py` is the only module both sides import.

```
media_preview_generator/nodes/
  protocol.py        wire contract shared by both sides: versions, headers, routes, statuses, error kinds, op names
  server/            runs ON a node (MODE=node). Never imported by the app's web process.
    app.py           create_node_app(): auth, /v1/health, /v1/info, /v1/access-check, task routes, role mounting
    identity.py      node id + key (<CONFIG_DIR>/node.json, 0600; NODE_KEY / AGENT_TOKEN env override the key)
    tasks.py         task table: start, poll (progress + log lines), cancel, artifacts, expiry when unpolled
    access.py        app path -> node path, roots guard, size + mtime_ns identity check
    info.py          GPU detection (cached, re-checked every 5 min and after a GPU error), CPU/RAM/temp, FFmpeg
    ops.py           op registry: the app's own heavy functions, called with node paths
    plex_db.py       the Plex database role (today's agent routes, moved; same protocol, same tests)
    winplatform.py   Windows replacements for the Linux-only bits the Plex database role needs
    wsgi.py          gunicorn entry point
  client.py          NodeClient: the app's HTTP client for one node
  registry.py        NodeRegistry: configured nodes, their live state, the health loop, alerts
  mappings.py        app path <-> node path rules, Test access sampling and suggestions
  readiness.py       node Setup Health rows (same buckets as server Setup Health)
  executor.py        runs one op on a node for a remote worker: start, poll, log lines, cancel, errors
media_preview_generator/web/routes/api_nodes.py      REST API behind the Nodes page
media_preview_generator/web/templates/nodes.html     Nodes page (+ static/js/nodes.js)

nodes/                         how to RUN each node type (packaging only, no app logic)
  README.md                    which node to run where
  worker/docker-compose.yml    the main image in node mode (MODE=node)
  plex-db/                     slim Linux image for the Plex database role (was plex-marker-agent/)
  windows/                     native Windows build of the Plex database role (PyInstaller + Startup install)
```

`plex-marker-agent/` moves to `nodes/plex-db/`. Its published image name (`ghcr.io/<owner>/plex-marker-agent`) and
release tag prefix (`marker-agent-v*`) stay until the owner decides a rename at release time, so Watchtower users
are not stranded.

## 2. Who does what

| | App | Node |
|---|---|---|
| Queue, schedules, groups, retries, decisions | ✓ | – |
| Tokens (Plex/Emby/Jellyfin), server API calls | ✓ | never |
| Writes (BIF, sidecars, trickplay, markers.db, Plex DB via its own node) | ✓ (or its Plex DB node) | only its own temp dir |
| ffprobe, Plex bundle hash, checks | ✓ | – |
| Heavy steps: frames, loudness, fingerprint, end-picture, credits | local workers | worker nodes |
| Path mappings | stored + sent by the app | applied + guarded by the node |

A node never receives a server token, never decides work, never receives a write path.

## 3. Protocol (node protocol 1)

Auth on every route except `GET /v1/health`: `Authorization: Bearer <key>`, compared with `secrets.compare_digest`
on bytes. Node routes also require `X-MPG-Node-Protocol: 1`; a mismatch answers 409 `{"error":{"kind":"protocol"}}`.
The Plex database routes keep the agent's header and protocol exactly.

Every JSON answer: `{"ok": bool, "node": NODE_BLOCK, ...}` where
`NODE_BLOCK = {"id", "name", "version", "protocols": [1], "roles": ["worker", "plex_db"]}` (`version` = the app
package version; the Plex database role also keeps the agent's `"agent"` block for old apps).

| Route | Body | Answer |
|---|---|---|
| `GET /v1/health` | – | `{"ok": true, "node": …}` (no auth; container HEALTHCHECK) |
| `GET /v1/info` | – | `node`, `platform {os, arch, python}`, `worker {gpus[], cpu {cores, load1, ram_total, ram_available}, temp {path, free_bytes}, busy, ffmpeg {path, version}, gpu_checked_at}`, `plex_db {config_dir, db_present, machine_identifier}` (per role) |
| `POST /v1/gpus/rescan` | – | same `worker.gpus` after a fresh detection |
| `POST /v1/access-check` | `{"mapping": [...], "files": [{"path", "size", "mtime_ns"}]}` | `{"results": [{"path", "ok", "node_path", "kind", "message"}], "nearby": {node_dir: [names…]}}` |
| `POST /v1/tasks` | `TASK_SPEC` | 202 `{"id"}` (the spec's `client_task_id` when it has one) |
| `GET /v1/tasks/<id>?log_from=N` | – | `TASK_STATE`; 404 `{"error":{"kind":"unknown_task"}}` when the node doesn't know it |
| `POST /v1/tasks/<id>/cancel` | – | `{"ok": true}` |
| `POST /v1/tasks/<id>/pause` / `resume` | – | `{"ok": true}` — the op's `pause_check()` follows it, exactly as a local pause |
| `GET /v1/tasks/<id>/artifact` | – | the task's artifact (preview: a tar of the JPEGs) |
| `DELETE /v1/tasks/<id>` | – | frees the task's temp dir |
| `GET /v1/log?lines=N` | – | the node's last N log lines |

`gpus[]` entries: `{"type", "device", "name", "status", "error"}` — the same tuple `gpu.detect.detect_all_gpus()`
returns, made JSON-safe.

`TASK_SPEC = {"op", "path", "size", "mtime_ns", "mapping": [{"app_prefix", "node_prefix"}], "gpu": {"type",
"device"} | null, "params": {...}, "client_task_id"?}` — `path` is the **app's** path; the node maps it.
`client_task_id` (optional, 16-64 of `[A-Za-z0-9_-]`; the app sends a fresh uuid hex per task) becomes the task id. A
repeated start with an id the node already has answers that task and starts nothing, so a start whose answer was lost
can't run twice. When the app's start times out after reaching the node, it sends `DELETE /v1/tasks/<client_task_id>`
(404 = it never started), so FFmpeg doesn't keep running there until the unpolled-task expiry.

`TASK_STATE = {"id", "op", "status", "progress": {"percent", ...}, "log": [{"seq", "level", "message"}],
"next_log": int, "result": {...} | null, "error": {"kind", "message"} | null, "seconds"}`.

Statuses: `running`, `done`, `failed`, `cancelled`, `file_not_visible`.
Error kinds: `file_not_visible` (with `reason`: `unmapped` | `outside_roots` | `missing` | `different_file` |
`unreadable`), `codec_not_supported`, `gpu_failed`, `cancelled`, `failed`, plus request-level `auth`, `protocol`,
`unknown_task`, `request`.

Task lifetime: a task nobody has polled for 60 s is cancelled and its temp dir removed (an app that restarted never
leaves work running). A finished task's artifact is kept until `DELETE` or 10 minutes after its last poll.

### Ops

Each op is a function `(ctx: TaskContext, node_path: str, params: dict) -> dict` registered in `server/ops.py`.
`TaskContext` gives `cancel_check()`, `pause_check()`, `progress(percent, **extra)`, `log(level, message)`, `temp_dir`,
`set_artifact(path)`, `gpu` (`(type, device)` or `None`).

| Op | Calls (unchanged app code) | Result |
|---|---|---|
| `preview.frames` | `processing.generator.generate_images` | `generate_images` tuple fields + artifact tar of JPEGs |
| `loudness.analyze` | `loudness.analyze.run` / `run_many` | the analysis result, JSON-safe |
| `intro.fingerprint` | `markers.audio.fingerprint.compute_fingerprint` | points as base64 little-endian u32 |
| `intro.end_picture` | `markers.audio.end_picture.decode_frames` | frames as base64 f32 + shape |
| `credits.find` | `markers.credits.detector.find_credits` with the node's own text-detection pool | result + CPU notes |

`CodecNotSupportedError` → error kind `codec_not_supported`; the app re-raises the same exception so its existing CPU
retry runs (on the **node's** CPU, see §6).

## 4. Settings

* New top-level `nodes`: `[{"id", "node_uid", "name", "url", "key", "enabled", "path_mappings": [{"app_prefix",
  "node_prefix"}], "added_at"}]`. `id` is assigned by the app (stable; members and Plex servers reference it).
  `node_uid` is the id the node reports, recorded on first contact; a different `node_uid` answering at the same
  address is refused ("a different node answered at this address"). A legacy helper has no `node_uid`.
* Worker group members gain `"node": <node id> | null` (null = this app). A node member's `device` is the node's
  device string. Resource keys become `node:<id>:gpu:<device>` / `node:<id>:cpu`.
* Plex servers: `markers.plex.agent` gains `"node_id"`. Migration v24 turns every enabled helper (`url` + `token`)
  into a `nodes` entry with the Plex database role and points the server's `agent.node_id` at it. The client resolves
  url + key from the node, so one record holds the address and key.

## 5. Node states (app side)

`connecting`, `ready`, `offline`, `update_needed`, `key_refused`, `access_problem`, `gpu_lost`, `disabled`.

* Health check every 30 s, backing off to 5 min while offline; "Check now" forces one.
* `update_needed`: node version ≠ app version for the worker role (exact match); for the Plex database role, the
  agent's existing `MIN_AGENT_VERSION` rule. An old helper that has no `/v1/info` is a Plex database node in
  "legacy" mode — it keeps working and Setup Health recommends updating it.
* Capacity: only `ready` nodes give workers; `gpu_lost` keeps CPU members; every other state is zero capacity with
  the reason shown on the member.
* Each state change raises or clears a bell notification naming the node and the one action.

## 6. Running a file on a node (remote workers)

* A remote worker is an ordinary worker (same thread, same group membership, same job kinds) whose heavy step goes
  through `nodes.executor`. All app-side work (checks, ffprobe, hashing, BIF build, publishing) runs in the app as
  today. The seam is a per-thread "where do heavy steps run" value set by the worker; each of the five call sites
  asks it and either calls the local function or `executor.run(op, …)`.
* While a task runs the worker polls every 1 s; node log lines are written into the job log by the worker's own
  thread (job-log routing is by registered thread id), prefixed `<node>/<device> ·`.
* **Node drops** (3 failed polls ≈ 12 s, or the node no longer knows the task): `NodeUnavailableError` → the item
  goes back in the queue once; a second drop on the same item fails it ("node unavailable twice"). A start the node
  never took (connection refused, no answer before an id) isn't a drop: the item goes back without counting (at most
  5 times, then it fails). After either, the node gets no new file until the registry's next check of it comes back
  ready (60 s at most), and the item's retry goes to another node or this app when one is free.
* **File not visible**: the item goes to the next eligible worker on a different node (or this app); when every
  eligible worker has failed it, the item fails "not visible on X, Y". Three different files not visible on one node
  within 10 minutes → node state `access_problem` (workers paused, alert).
* **GPU failure / codec not supported** on a node: retry on the same node's CPU, never on the app's. GPU lost
  (detection fails after a GPU error) → state `gpu_lost`.
* Cancel and pause act on the node task: the executor forwards the job's cancel and pause state to
  `POST cancel` / `pause` / `resume`, so a paused job pauses FFmpeg on the node exactly as it does locally.

## 7. Windows (Plex database role)

Self-contained PyInstaller build (~25 MB), started from the user's Startup entry (`HKCU\...\Run`, no admin), running
as the user Plex runs as. Default Plex folder `%LOCALAPPDATA%\Plex Media Server`. `winplatform.py` replaces: the
`fcntl` lock probe (→ `LockFileEx` on `-shm` byte 128, plus Restart Manager to list holders), the mountinfo
local-disk check (→ `GetDriveTypeW`: 3 = fixed passes, 4 = remote refused), owner-uid checks (→ "same user"), POSIX
signal names. Known porting failures from CI (§9): SQLite URI with special characters, Windows `D:\` paths in marker
item lookups.

## 8. Build order and lanes

1. **Foundation** (controller): `nodes/protocol.py`, `nodes/client.py`, this spec, repo layout.
2. **Lane A — node server**: `nodes/server/{app,identity,tasks,access,info,wsgi}.py`, `MODE=node` in `wrapper.sh`,
   `nodes/worker/docker-compose.yml`.
3. **Lane B — app-side nodes**: settings + v24 migration, `registry.py`, `mappings.py`, `readiness.py`,
   `api_nodes.py`, Nodes page, notifications, Plex database selector.
4. **Lane C — remote execution**: `executor.py`, `server/ops.py`, the five call-site seams, node members in worker
   groups, requeue / not-visible / access-problem rules, worker names, job log lines.
5. **Lane D — Plex database role**: move the agent into `server/plex_db.py`, `nodes/plex-db/` packaging, loudness
   writes + Library Health counts through the node, `winplatform.py`, `nodes/windows/`, Windows CI.
6. Integration proof on storage (app container + node containers, real files), docs.

## 9. Evidence summary

See `evidence/` for raw output. Prototype (app image + a small node API, real 22-min 1080p HEVC episode, app at
`/data`, node at `/mnt/nas`): previews CPU 274 frames, identical JPEGs and BIF, 217.6 s app vs 207.5 s node;
NVIDIA identical, 52.5 s vs 45.8 s, 4.5 MB tar; loudness, fingerprint (3,845 pts), end-picture (60 frames) and
credits identical. Cancel stopped FFmpeg in 0.63 s. Killed node detected after 12 s. Size + mtime_ns identical
across ZFS / NFS 4.2 / bind / mergerfs; dev + inode differ. Requeue spike: one drop → 3/3 done, two drops → exactly
one failed row; 1,119 existing tests green. Windows runner: WAL holds byte 128 of `-shm`; `LockFileEx` probe fails
(error 33) while open, a new connection during the probe waits ~1.5 s and succeeds; GetDriveTypeW C:=3, subst=3,
mapped=4, UNC=4; 25.2 MB PyInstaller build served `/v1/info`; non-admin `schtasks ONLOGON` = Access is denied; app
Plex DB tests on Windows with a shim: 874 passed / 39 failed (23 test-simulation, 16 real porting).

## 10. Interfaces between lanes (binding)

Lanes build in parallel against these signatures. Change one only by updating this section first.

### 10.1 Node server (Lane A) ⇄ ops (Lane C) ⇄ Plex database role (Lane D)

```python
# nodes/protocol.py (foundation; both sides import these)
class TaskContext(Protocol):             # one per task, handed to the op; Lane A's task table implements it
    gpu: tuple[str, str] | None         # (type, device) on THIS node, or None for CPU
    temp_dir: str                       # private, removed with the task
    def cancel_check(self) -> bool: ...
    def pause_check(self) -> bool: ...
    def progress(self, percent: float, **extra: object) -> None: ...
    def log(self, level: str, message: str) -> None: ...   # also: loguru lines from the op's thread land here
    def set_artifact(self, path: str) -> None: ...          # served by GET /v1/tasks/<id>/artifact
OpFn = Callable[[TaskContext, str, dict], dict]             # (ctx, node_path, params) -> JSON-safe result
class OpError(Exception):                # raise from an op to finish with a specific error kind
    def __init__(self, kind: str, message: str, **extra: object) -> None: ...

# nodes/server/app.py (Lane A)
def create_node_app(*, config_dir: str | None = None, roles: list[str] | None = None,
                    ops: Mapping[str, OpFn] | None = None, key: str | None = None) -> Flask
# roles default: NODE_ROLES env (comma list, default "worker"); ops default: nodes.server.ops.OPS (lazy import).
# When ROLE_PLEX_DB is in roles: nodes.server.plex_db.register_plex_db(app, plex_config_dir=..., key=...)

# nodes/server/ops.py (Lane C)
OPS: dict[str, OpFn]                     # keys = protocol.OPS; CodecNotSupportedError -> OpError(KIND_CODEC_NOT_SUPPORTED)

# nodes/server/plex_db.py (Lane D)
AGENT_VERSION: str
def agent_block() -> dict                # the agent's {"version", "protocols", "capabilities"}; /v1/health carries it
def register_plex_db(app: Flask, *, plex_config_dir: str, key: str, mountinfo_path: str = "/proc/self/mountinfo") -> None
# Adds today's agent routes (/v1/ping, /v1/checks/*, /v1/chapters/*, /v1/item/*, /v1/items/shown, + new loudness and
# library-health routes) with the agent's own protocol header check. /v1/health is Lane A's and also carries the
# agent block when this role is on, so old apps keep reading it.
```

Import discipline: `nodes/server/app.py` imports only Flask, loguru, `nodes.protocol` and its own small modules at
module scope. Worker-role modules (GPU detection, FFmpeg, numpy, ops) are imported only when the worker role is on,
so the slim Plex database builds (Linux image without the app's dependencies, Windows exe) can run `create_node_app`.

Env: `MODE=node` (wrapper.sh), `NODE_PORT` (default 9494), `NODE_ROLES`, `NODE_NAME` (default hostname),
`NODE_KEY` (else `AGENT_TOKEN`, else generated once into `<CONFIG_DIR>/node.json`), `PLEX_CONFIG_DIR` (plex_db).

### 10.2 Registry (Lane B) used by remote execution (Lane C)

```python
# nodes/registry.py (Lane B)
STATE_CONNECTING, STATE_READY, STATE_OFFLINE, STATE_UPDATE_NEEDED, STATE_KEY_REFUSED,
STATE_ACCESS_PROBLEM, STATE_GPU_LOST, STATE_DISABLED: str
@dataclass(frozen=True)
class NodeView:
    id: str; name: str; url: str; enabled: bool; state: str; reason: str; version: str
    roles: tuple[str, ...]; gpus: tuple[dict, ...]   # {"type","device","name","status"}
    cpu_cores: int; legacy: bool; last_seen: float | None
@dataclass(frozen=True)
class NodeCapacity:
    cpu: bool; gpu: bool; reason: str                # reason shown on a member with zero workers
def get_registry() -> NodeRegistry                     # process singleton
class NodeRegistry:
    revision: int                                      # bumps on any state/capacity change
    def views(self) -> list[NodeView]: ...
    def view(self, node_id: str) -> NodeView | None: ...
    def client(self, node_id: str) -> NodeClient: ...  # KeyError for an unknown node
    def mappings(self, node_id: str) -> list[PathMapping]: ...
    def capacity(self, node_id: str) -> NodeCapacity: ...  # unknown/removed node -> NodeCapacity(False, False, "node removed")
    def report_unavailable(self, node_id: str) -> None: ...            # re-check now
    def report_not_visible(self, node_id: str, app_path: str, reason: str) -> None: ...  # 3 files/10 min -> access_problem
    def report_gpu_error(self, node_id: str, device: str) -> None: ...  # rescan; failed/missing -> gpu_lost
    def start(self) -> None: ...; def stop(self) -> None: ...          # health loop thread
```

### 10.3 Remote execution (Lane C)

```python
# nodes/executor.py
@dataclass(frozen=True)
class Placement:
    node_id: str; node_name: str
def current_placement() -> Placement | None          # set per worker thread; None = run locally
@contextmanager
def placed_on(placement: Placement | None) -> Iterator[None]
class NodeUnavailableError(Exception): ...           # node stopped answering mid-task (or forgot the task)
class FileNotVisibleError(Exception):                # .node_id, .node_name, .reason
def run_on_node(op: str, app_path: str, params: dict, *, gpu: tuple[str, str] | None,
                cancel_check, pause_check=None, progress_callback=None,
                artifact_to: str | None = None) -> dict
```

Worker group member: `{"id", "resource", "device", "count", "job_types", "node": str | None}`; resource key
`node:<id>:cpu` / `node:<id>:gpu:<device>` for node members, unchanged for local ones.

### 10.4 Nodes REST API (Lane B backend ⇄ Lane B UI)

All under `/api`, `@api_token_required`: a signed-in session or the API token, even before setup is complete (the
setup wizard doesn't use these routes, and they send stored keys out). A node's key never comes back; posting `"****"`
keeps the stored key, but only for the address it was saved with: a probe or `PUT` that names another address with
`"****"` (or, for `PUT`, no key) is a 400 asking for the key to be typed. Errors are `{"error": "<sentence>"}` with 400
(bad input), 404 (unknown node) or 502 (the node couldn't be asked).

| Route | Body | Answer |
|---|---|---|
| `GET /nodes` | – | `{"nodes": [NODE_ROW], "app_version"}` |
| `POST /nodes/probe` | `{url, key, id?}` | `{"ok": true, "info": PROBE_INFO}` or `{"ok": false, "error": {kind, message}}` — Add node step 1, nothing saved. With `id`, `"****"` means that node's stored key when `url` is its saved address (400 otherwise) |
| `POST /nodes` | `{name, url, key, path_mappings}` | 201 `NODE_ROW`; 409 `{"error", "node_id", "name"}` when the node answering there is already added |
| `PUT /nodes/<id>` | any of `{name, url, key, enabled, path_mappings}` | `NODE_ROW`; a new `url` needs a typed `key` and clears `node_uid` (recorded again on first contact) |
| `DELETE /nodes/<id>` | – | `{"ok": true}`; 409 `{"error", "used_by": [{kind: "group" \| "server", id, name}]}` while a group member or Plex server uses it |
| `POST /nodes/<id>/check` | – | `NODE_ROW` after a fresh health check |
| `POST /nodes/<id>/test-access` | `{path_mappings?}` (default: the saved rows) | `{"libraries": [LIBRARY_ACCESS]}`; a test of the saved rows is remembered for Setup Health |
| `GET /nodes/<id>/health` | – | Setup Health envelope `{overall_ok, sections}` (same shape as server Setup Health) |
| `GET /nodes/<id>/log?lines=N` | – | `{"lines": [{time, level, message}]}`, oldest first (`time` ISO 8601) |
| `POST /nodes/<id>/rescan-gpus` | – | `NODE_ROW` |

`NODE_ROW = {id, name, url, enabled, state, reason, version, roles, legacy, platform: {os, arch, python}, gpus:
[{type, device, name, status, error}], cpu: {cores, load1, ram_total, ram_available}, busy, last_seen, path_mappings,
used_by: {groups: [{id, name}], servers: [{id, name}]}}`. `busy` = how many tasks the node is running (int);
`last_seen` = epoch seconds of the last answer, or null; `error` is `""` for a working GPU. Facts the node hasn't
reported yet (before its first answer) are empty: `roles: []`, `platform: {}`, `gpus: []`, `cpu` values null.

`PROBE_INFO` is `/v1/info` projected for Add node:

```
{"node": {id, name, version, roles},          # id null and name "" for an older Plex helper
 "legacy": bool,                              # an older Plex helper (no /v1/info; answered its /v1/ping)
 "platform": {os, arch, python},              # {} for a helper
 "worker": {gpus: [{type, device, name, status, error, credit_text?}], cpu: {...}, temp: {path, free_bytes},
            notes: [{title, chip, message}]} | null,   # null without the worker role; notes = credits text on CPU
 "plex_db": {config_dir, db_present, machine_identifier} | null,
 "version_match": bool | null,                # null without the worker role (only a worker must match exactly)
 "already_added": {id, name} | null,          # another saved node with this node id
 "suggested_path_mappings": [{app_prefix, node_prefix}]}   # one row per library folder, same path; [] if no worker
```

Probe error kinds: `auth` (key refused), `unreachable` (no answer), `protocol` (answered, but not as a node or Plex
helper this app can use), `request` (the node refused the request as malformed), `different_node` (with `id`: another
node than the saved one answers there).

`LIBRARY_ACCESS = {server, library, app_root, checked, ok, files: [{path, ok, kind, message, node_path}], nearby:
{node_dir: [names]}, suggestion: {from, to} | null, error: str | null}`. `kind` is a `NOT_VISIBLE_*` reason (`""` when
seen); `nearby` holds the node's listings of the deepest existing folder above this library's missing files;
`suggestion` swaps the covering row's `node_prefix` (`from`, `to` are node-side paths); `error` says why no file was
sampled (folder missing or no video near its top), with `checked: 0`.

Node Setup Health rows are server Setup Health rows; a failing one also says where its fix is: `fix_tab`
(`media` / `settings` / `overview`, the node dialog tab) with `fix_label` and, on a media row, `suggestion: {from, to,
app_root}` (applied, then tested) or `fix_action: "test_access"`; `fix_where: "node"` (only the node's machine can
fix it: version, GPU, Plex database); or `help_url` + `help_label` (a page of this app). Passing rows carry none.
