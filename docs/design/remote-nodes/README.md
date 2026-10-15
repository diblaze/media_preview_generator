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

## 0.1 Build status (2026-10-15)

Built on `stevezau/extra_node` **before the owner reviewed this design** (owner: "build everything, test it, prove it,
I'll review the design later and may ask for changes"). Not pushed to `dev`, not deployed, not released.

* **Built:** every lane in §8: node server + `MODE=node`, Nodes page and API, node devices in worker groups, remote
  execution for previews / Intro & Credits / loudness, the Plex database role (old helper folded in, plus loudness
  writes and Library Health rows), the Windows Plex database node (`nodes/windows/`), migration v24, docs
  (`docs/guides.md#remote-nodes`).
* **Reviewed:** an Architecture Review per area (jobs, settings/auth, Plex database role), then one over all the fixes;
  every HIGH/MED/LOW from those is fixed.
* **Proven:** the full unit suite (20,398 passed, 92% coverage), node + worker-group e2e, Windows CI (1,052 Plex
  database tests on `windows-latest`, PyInstaller build started and called), and a real lab run with GPU, CPU and
  Plex database nodes (all scenarios passed: [`evidence/lab-final/`](evidence/lab-final/README.md)).
* **Changes from the design as written:**
  - A node that drops a file is held back until a check begun after the drop finds it ready (or 5 min); starts
    the node never took don't count toward the two-drop limit (max 5 per file). Retries prefer another worker.
  - Each task carries a client id; a start whose answer was lost is cancelled by that id.
  - Node log lines carry an epoch `ts` so the app shows them in its own time zone.
  - Test access results persist in `node_access_results.json` until that node's mappings change. A test of rows
    not saved yet (Add node, the Media tab) is kept once exactly those rows are saved.
  - `/api/nodes*` needs sign-in even before setup; a saved key is only ever sent to its saved address.
  - v24 moves the old helper's `url`/`token` into the node and drops them from the server. Rolling back means
    restoring `settings.json.pre-v<N>.bak` (written beside settings.json before migrating): an older build
    refuses a v24 file.
  - A worker node that answers with the saved key at the saved address is that node even when it reports a new
    id (recreated without its `/config`): the new id is recorded and logged, nothing is refused. A Plex database
    node's new id is taken silently only when its `plex_db.machine_identifier` matches every server that uses it;
    otherwise its Plex database work is refused ("Type the node's key again on its Settings tab to confirm") until
    the key is typed again, which clears the saved id. A Plex database node beside the wrong Plex is caught by the
    Plex machine identifier check.
  - The Plex database node needs a `/config` volume for loudness (the undo record lives there); without one it
    refuses loudness writes.
* **Still open:**
  - The image name `plex-marker-agent` and tag prefix `marker-agent-v*` are unchanged; renaming is a release-time
    decision.
  - The Windows node has never touched a real Plex on Windows (needs the #407 reporter's beta), and Docker Desktop
    reaching a native node via `host.docker.internal` is untested.
  - Nodes in a different time zone from the app weren't tried in the lab (all ran one zone).
  - A worker node must run the app's exact version, so every app update means updating each worker node.

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
    access.py        app path -> node path, roots guard, size + mtime identity check (mtime within 2 s)
    info.py          GPU detection (cached, re-checked every 5 min and after a GPU error), CPU/RAM/temp, FFmpeg
    ops.py           op registry: the app's own heavy functions, called with node paths
    plex_db.py       the Plex database role (today's agent routes, moved; same protocol, same tests)
    winplatform.py   Windows replacements for the Linux-only bits the Plex database role needs
    wsgi.py          entry point (waitress)
  client.py          NodeClient: the app's HTTP client for one node
  registry.py        NodeRegistry: configured nodes, their live state, the health loop, alerts
  mappings.py        app path <-> node path rules, Test access sampling and suggestions
  readiness.py       node Setup Health rows (same buckets as server Setup Health)
  access_results.py  each node's last Test access, kept across restarts (<CONFIG_DIR>/node_access_results.json)
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
`NODE_BLOCK = {"id", "name", "version", "build", "protocols": [1], "roles": ["worker", "plex_db"]}` (`version` = the
app package version, `build` = the image's `GIT_SHA` or "": a worker node on the same version but another build needs
an update, since pull request and local images all report one version; the Plex database role also keeps the agent's
`"agent"` block for old apps).

| Route | Body | Answer |
|---|---|---|
| `GET /v1/health` | – | `{"ok": true, "node": …}` (no auth; container HEALTHCHECK) |
| `GET /v1/info` | – | `node`, `platform {os, arch, python}`, `worker {gpus[], cpu {cores, load1, ram_total, ram_available}, temp {path, free_bytes}, busy, ffmpeg {path, version}, gpu_checked_at, gpu_error}`, `plex_db {config_dir, db_present, machine_identifier}` and `agent {version, protocols, capabilities}` (per role) |
| `POST /v1/gpus/rescan` | – | same `worker.gpus` after a fresh detection |
| `POST /v1/access-check` | `{"mapping": [...], "files": [{"path", "size", "mtime_ns"}]}` | `{"results": [{"path", "ok", "node_path", "kind", "message"}], "nearby": {node_dir: [names…]}}` |
| `POST /v1/tasks` | `TASK_SPEC` | 202 `{"id"}` (the spec's `client_task_id` when it has one) |
| `GET /v1/tasks/<id>?log_from=N` | – | `TASK_STATE`; 404 `{"error":{"kind":"unknown_task"}}` when the node doesn't know it |
| `POST /v1/tasks/<id>/cancel` | – | `{"ok": true}` |
| `POST /v1/tasks/<id>/pause` / `resume` | – | `{"ok": true}` — the op's `pause_check()` follows it, exactly as a local pause |
| `GET /v1/tasks/<id>/artifact` | – | the task's artifact (preview: a tar of the JPEGs) |
| `DELETE /v1/tasks/<id>` | – | frees the task's temp dir |
| `GET /v1/log?lines=N` | – | `{"lines": [{time, ts, level, message}]}`, the node's last N lines oldest first: `time` ISO 8601 with the node's offset, `ts` epoch seconds (added later; an older node sends no `ts`) |

Same file: the size must match exactly and the mtime must be less than 2 s from the app's `mtime_ns`. A node may
read the media through a filesystem that keeps coarser timestamps than the app's (SMB2/3: 100 ns; sshfs, SMB1: 1 s;
FAT: 2 s), so an exact `mtime_ns` match would refuse every file there. The same rule (`access.same_mtime`) applies to
Test access and to every task.

`gpus[]` entries: `{"type", "device", "name", "status", "error"}` — the same tuple `gpu.detect.detect_all_gpus()`
returns, made JSON-safe.

`TASK_SPEC = {"op", "path", "size", "mtime_ns", "mapping": [{"app_prefix", "node_prefix"}], "gpu": {"type",
"device"} | null, "params": {...}, "client_task_id"?}` — `path` is the **app's** path; the node maps it.
`client_task_id` (optional, 16-64 of `[A-Za-z0-9_-]`; the app sends a fresh uuid hex per task) becomes the task id. A
repeated start with an id the node already has answers that task and starts nothing, so a start whose answer was lost
can't run twice. When the app's start may have reached the node (no answer in time, the connection dropped before
the answer, or a 5xx from a gateway in front of it) it sends `DELETE /v1/tasks/<client_task_id>` from a thread of its
own (404 = it never started), so FFmpeg doesn't keep running there until the unpolled-task expiry. The node remembers an id it was asked to delete before knowing it (5 minutes, at most 1024 ids):
a start arriving after that DELETE answers 409 `task_deleted` and runs nothing.

A stopping app sends `DELETE /v1/tasks/<id>` for every task it still runs on a node, all at once and waiting 1 s at
most, so none runs on until the unpolled-task expiry beside the tasks the job revived on the next start sends.

`TASK_STATE = {"id", "op", "status", "progress": {"percent", ...}, "log": [{"seq", "level", "message"}],
"next_log": int, "result": {...} | null, "error": {"kind", "message"} | null, "seconds"}`.

Statuses: `running`, `done`, `failed`, `cancelled`, `file_not_visible`.
Error kinds: `file_not_visible` (with `reason`: `unmapped` | `outside_roots` | `missing` | `different_file` |
`unreadable`), `codec_not_supported`, `gpu_failed`, `cancelled`, `failed`, plus request-level `auth`, `protocol`,
`unknown_task`, `request`, `task_deleted`.

Task lifetime: a task nobody has polled for 60 s is cancelled and its temp dir removed (an app that restarted never
leaves work running). A finished task's artifact is kept until `DELETE` or 10 minutes after its last poll; a `DELETE` nothing answered is
sent again 1 s and 4 s later.

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
  `node_uid` is the id the node reports, recorded on contact; a worker node that reports a new one
  (reinstalled) is still that node, and the new id replaces the old (a Plex database node: see §0.1). A new address
  or the key typed again clears it. Add node and an address change refuse an address another
  saved node has, or where an added node answers. A legacy helper has no `node_uid`.
* Worker group members gain `"node": <node id> | null` (null = this app). A node member's `device` is the node's
  device string. Resource keys become `node:<id>:gpu:<device>` / `node:<id>:cpu`.
* Plex servers: `markers.plex.agent` becomes `{"enabled", "node_id"}`. Migration v24 turns every helper (`url` +
  `token`) into a `nodes` entry with the Plex database role (turned on only when a server uses it: helper, Intro &
  Credits and server all on) and points the server's `agent.node_id` at it. A switched-on helper it can't move
  (no key, bad address) stays on with no node, so it is refused rather than opening this app's own copy. The
  client resolves url + key from the node, so one record holds the address and key. A server with its node
  switched off doesn't count as using it (Remove clears its remembered `node_id`).

## 5. Node states (app side)

`connecting`, `ready`, `offline`, `update_needed`, `key_refused`, `access_problem`, `gpu_lost`, `no_ffmpeg`,
`disabled`.

* Health check every 30 s, backing off to 5 min while offline; "Check now" forces one.
* `update_needed`: node version ≠ app version for the worker role (exact match); for the Plex database role, the
  agent's existing `MIN_AGENT_VERSION` rule (read from `/v1/info`'s `agent` block). A node that refuses the app's
  node protocol (409 with its `node` block) is `update_needed` naming both versions. An old helper that has no
  `/v1/info` is a Plex database node in "legacy" mode — it keeps working and Setup Health recommends updating it.
* `no_ffmpeg`: a worker node whose `/v1/info` reports `worker.ffmpeg.path` null; it gets no work.
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
* **Node drops** (polls failing for 12 s, at least 3 in a row, or the node no longer knows the task):
  `NodeUnavailableError` → the item goes back in the queue once (the node is still asked to drop the task, so one
  that comes back stops it); a second drop on the same item fails it ("node <name> unavailable (it dropped the
  file twice)", or "nodes A and B unavailable (each dropped the file once)"; a webhook job counts it failed, never "not found on any server"). A start the node never took
  (connection refused, no answer before an id) isn't a drop: the item goes back without counting (at most 5 times,
  then it fails). After either, the node gets no new file until a registry check begun after the drop comes back
  ready (5 min at most), and the item's retry waits for another node or this app while one of those runs its kind
  now (busy or not); it goes back to the same node only when none does. Health checks
  read `/v1/info` with a 10 s timeout, so a node that takes connections but never answers reads offline ("accepts
  connections but doesn't answer") within a minute.
* **File not visible** (missing, or not the same file: another size, or an mtime 2 s or more from the app's): the
  item goes to the next eligible worker on a different node (or this app); when every eligible worker has failed it,
  the item fails "not visible on X, Y". Members on a node that is offline or connecting (or rechecking a lost GPU)
  still count, since it comes back on its own; members that wait on someone (node turned off, update needed, key
  refused, access problem, no worker role, GPU not listed) don't, and the failure names them: "not visible on X; no
  other worker can take it: Z (node needs an update)". Three different files not visible on one node within 10
  minutes → node state `access_problem` (workers paused, alert). It pauses every library on that node, not only the
  ones it missed (one wrong row makes the others suspect; files it can't see run elsewhere anyway), and its reason
  names the libraries those files are in. It lives in memory: an app restart clears it until three more misses.
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
| `PUT /nodes/<id>` | any of `{name, url, key, enabled, path_mappings}` | `NODE_ROW`; a new `url` needs a typed `key` and clears `node_uid` (recorded again on first contact); 409 like `POST` when another saved node has that address or answers there |
| `DELETE /nodes/<id>` | – | `{"ok": true}`; 409 `{"error", "used_by": [{kind: "group" \| "server", id, name}]}` while a group member or a Plex server with its node switched on uses it |
| `POST /nodes/check` | – | 202 `{"nodes": [NODE_ROW], "app_version"}` at once: a health check of every turned-on node runs in the background (the Nodes page's Check now reads the list until no row is `checking`) |
| `POST /nodes/<id>/check` | – | 202 `NODE_ROW` at once; the health check runs in the background, and one already running or queued for the node answers it |
| `POST /nodes/<id>/test-access` | `{path_mappings?}` (default: the saved rows) | `{"libraries": [LIBRARY_ACCESS]}`; a test of the saved rows is remembered for Setup Health, across restarts, until the node's mappings change |
| `GET /nodes/<id>/health` | – | Setup Health envelope `{overall_ok, sections}` (same shape as server Setup Health) |
| `GET /nodes/<id>/log?lines=N` | – | `{"lines": [{time, level, message}]}`, oldest first (`time` ISO 8601 in this app's time zone, from the node's `ts`, else its `time`) |
| `POST /nodes/<id>/rescan-gpus` | – | `NODE_ROW` |

`NODE_ROW = {id, name, url, enabled, state, reason, version, roles, legacy, platform: {os, arch, python}, gpus:
[{type, device, name, status, error}], cpu: {cores, load1, ram_total, ram_available}, busy, last_seen, checking, path_mappings,
used_by: {groups: [{id, name}], servers: [{id, name}]}, to_fix: {must_fix, recommended}}`. `to_fix` counts the
node's Setup Health rows to fix (without the Plex database checks, which ask the node). `busy` = how many tasks the node is running (int);
`last_seen` = epoch seconds of the last answer, or null; `checking` = a health check is running or queued for it; `error` is `""` for a working GPU. Facts the node hasn't
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
helper this app can use; a node on another node protocol adds `node_version`), `request` (the node refused the
request as malformed).

`LIBRARY_ACCESS = {server, library, app_root, checked, ok, files: [{path, ok, kind, message, node_path}], nearby:
{node_dir: [names]}, suggestion: {from, to} | null, error: str | null, empty: bool}`. `kind` is a `NOT_VISIBLE_*` reason (`""` when
seen); `nearby` holds the node's listings of the deepest existing folder above this library's missing files inside
the mapped roots or, when the row's node folder itself doesn't exist, the folder names (no files) in its parent, never
higher; `suggestion` swaps the covering row's `node_prefix` for a listed folder named like the library folder
(exact name first, then ignoring case, then ignoring Unicode form; `from`, `to` are node-side paths); `error` says
why no file was sampled (folder missing, timed out, or no video near its top), with `checked: 0`; `empty` is true for
the last (an empty library is not a problem: its Setup Health row passes). One server folder mapped to several disks
reports only the disks that hold it, unless none does.

Node Setup Health rows are server Setup Health rows; a failing one also says where its fix is: `fix_tab`
(`media` / `settings` / `overview`, the node dialog tab) with `fix_label` and, on a media row, `suggestion: {from, to,
app_root}` (applied, then tested) or `fix_action: "test_access"`; `fix_where: "node"` (only the node's machine can
fix it: version, GPU, Plex database); or `help_url` + `help_label` (a page of this app). Passing rows carry none.
