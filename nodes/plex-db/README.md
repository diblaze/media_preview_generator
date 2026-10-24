# Plex database node

A small container you run **next to Plex**. It lets Media Preview Generator write into the database of a Plex that
runs on a different machine: Intro & Credits markers, chapter thumbnails (1.1.0 and newer), loudness measurements and
Library Health's database counts (1.2.0 and newer).

This is the container that used to be called the **Plex marker agent**. The image, key (`AGENT_TOKEN` still works),
port and address in the app are unchanged. An existing install keeps working with this app, and a 1.2.0 node keeps
working with an older app.

Plex on Windows without Docker: use the [Windows node](../windows/README.md) instead.

## Do you need it?

Only when the app and Plex run on different machines. If they run on the same machine, don't run this: the app writes
Plex's database itself.

Plex has no API for markers, so the app writes them straight into Plex's library database. SQLite's file locking only
works between processes on one machine. Over NFS, SMB or a Docker Desktop share it isn't safe, and the app refuses
("Plex's database is on a network share... Plex stays read-only"). So something has to do the write on the Plex
machine. That is this node. It is the only supported way to write markers or loudness to a Plex on another machine.

## What you need

- Docker (or Docker Compose) on the machine Plex runs on.
- The path of Plex's config folder on that machine.
- The user and group ID (`PUID` / `PGID`) Plex runs as. A node running as anyone else starts and looks healthy, then
  writes nothing.

## Install

1. Make a folder for the node and put [`docker-compose.yml`](docker-compose.yml) in it.
2. Create a `.env` file beside it:

   ```bash
   {
     printf 'NODE_KEY=%s\n' "$(openssl rand -hex 24)"
     printf 'PLEX_CONFIG=%s\n' /path/to/plex/config
     printf 'PUID=%s\nPGID=%s\n' 1000 1000
   } > .env
   chmod 600 .env
   ```

3. Start it: `docker compose up -d`.

Notes on `.env`:

- `PLEX_CONFIG` has no default. Leave it out and Compose stops with a message naming it.
- `NODE_KEY` is optional. Without it the node makes a key and prints it once in `docker logs plex-marker-agent`.
- `PUID` / `PGID` fall back to `1000`.

Without Compose, use `--env-file .env` so the key comes from the file. With an empty `NODE_KEY` the node makes a key the
app doesn't have.

```bash
# --user takes no .env value here; put the uid:gid that owns Plex's database in it yourself.
docker run -d --name plex-marker-agent --restart unless-stopped \
  --user 1000:1000 \
  --env-file .env \
  -e PLEX_CONFIG_DIR="/plex/Library/Application Support/Plex Media Server" \
  -v /path/to/plex/config:/plex \
  -v plex-db-node-config:/config \
  -p 9494:9494 \
  --security-opt no-new-privileges:true --cap-drop ALL \
  ghcr.io/stevezau/plex-marker-agent:1.2.0
```

## Add it in the app

1. Open **Settings → Nodes → Add node**.
2. Enter the address (`http://<this-machine>:9494`) and the same key.
3. Open **Servers → your Plex → Edit → Connection → Plex database** and choose the node.

You can choose the node there while Intro & Credits is off. Chapter thumbnails have their own checkbox in the
Processing tab and need neither marker consent nor Plex Pass.

## Settings

| Setting | What it is |
|---|---|
| `NODE_KEY` | The key both sides share: 16 to 200 characters of printable ASCII, no spaces (make one with `openssl rand -hex 24`). Never logged. Without it, the node makes a key on first start, keeps it in `/config/node.json` and prints it once in its log. To keep it in a file (a Docker secret), set `FILE__NODE_KEY` to the file's path. |
| `AGENT_TOKEN` | The key's old name. An install that set it keeps working (up to 200 characters, with a warning when it is short). New installs set `NODE_KEY`, which wins when both are set. `FILE__AGENT_TOKEN` works the same way. |
| `PLEX_CONFIG` | Plex's config folder **on this host**. Compose only (the `-v` source above). Required. |
| `PLEX_CONFIG_DIR` | Plex's config folder as **this container** sees it. The database path is derived from it. |
| `NODE_PORT` | The port to listen on (default 9494; `AGENT_PORT`, its old name, is used when `NODE_PORT` isn't set). It moves what the node binds inside the container. Compose derives the published port from it. With `docker run`, change `-p` to match. |
| `NODE_NAME` | The name the app shows (Compose default `plex-database`). |
| `NODE_ROLES` | Set to `plex_db` by the image. Leave it. |
| `AGENT_BIND` | The host address Compose publishes on (default `0.0.0.0`, all interfaces). Set it to the one LAN address the app reaches this machine on if the host has several. |
| `PUID` / `PGID` | The user that owns Plex's database, the same values Plex's own container runs as (default `1000`). Compose runs the node as them (`user:`). With `docker run`, use `--user <uid>:<gid>`. The node is non-root and can't write the database as anyone else. |

Keep three things in mind:

- **Same machine as Plex, same path.** The node proves Plex has the database open through the file it sees. A copy, a
  snapshot or a different mount of the same folder is refused.
- **One container per Plex**, with one worker inside it. Two writers would break the "one writer at a time" rule the
  app relies on.
- **The key is all that stands between a caller and Plex's database.** Keep the port on your LAN. Don't publish it to the
  internet.

## Update

1. Set the new version's tag in `docker-compose.yml`.
2. Run `docker compose pull && docker compose up -d`.

The `/config` volume stays. The node only has to be new enough for the app, not match its version. The app says when
it needs an update. Update the node before the app: a newer node still speaks the older app's protocol.

**Updating from before nodes?** The old compose file or `docker run` had no `/config` volume. Add
`-v plex-db-node-config:/config` (or the compose file's `volumes:` line), or loudness reports "Mount a volume at
/config". To move from the old Plex marker agent, replace the old compose file with this one and run
`docker compose up -d`: the service, container name, image and settings are the same.

## Remove

1. In the app, stop using the node: turn off **Plex database** for the server under **Servers → your Plex → Edit →
   Connection**. Then delete the node under **Settings → Nodes**. The app tells you if something still uses it.
2. `docker compose down` in the node's folder.

`docker compose down -v` also deletes the `/config` volume, with the loudness undo record in it. To undo loudness writes
afterwards, start the node again first (see [Undoing it](../../docs/plex-loudness-normalization.md#undoing-it); run the
command in this container).

## What it touches

It writes one file: `Plug-in Support/Databases/com.plexapp.plugins.library.db` inside the Plex config folder you mount.

- **Markers:** the marker rows of one item at a time (`taggings` on Plex's marker tag, and `pv:intros` / `pv:credits`
  in that item's `media_parts.extra_data`).
- **Chapter thumbnails:** only `taggings.thumb_url` on existing chapter rows, after checking every JPEG in the derived
  media bundle. It never creates chapters or changes marker data.
- **Loudness:** `ln:*` fields on audio streams' `media_streams.extra_data`, and an "analysed" mark in
  `metadata_items.extra_data`. It checks inside the write's transaction that the streams are still the ones the app
  measured.
- **Library Health:** fixed, read-only counting queries.

It also reads `Preferences.xml` in that folder, for one string: Plex's machine identifier. The app compares it with the
identifier its Plex reports, so a node set up beside the wrong Plex is refused before anything is written.

Outside Plex's folder it writes `/config/loudness-writes.jsonl` in its own volume: the undo record of each loudness
write. `/config` must be a named volume (the compose file names one, `plex-db-node-config`) or a mounted folder. Without
one, an update throws away the undo record and the node's id, so the node writes no loudness until it has one.

It will not:

- take a database path from the app (the path comes from this container's `PLEX_CONFIG_DIR`);
- run SQL the caller supplies, or answer anything outside its fixed operations;
- create Plex's marker tag row (if Plex hasn't made one, the write is refused and the app tells you to run Plex's own
  detection once);
- write a database that isn't on a local disk of this machine, or whose schema it doesn't recognise;
- do anything without the shared key.

It refuses exactly what the app's own writer refuses, because it runs that writer (`media_preview_generator/markers/publishers/plex_db.py`,
`media_preview_generator/loudness/plex_db.py`).

## When something is wrong

| What you see | What it means |
|---|---|
| **Can't reach it** | The node isn't running, or the address or port is wrong. Markers wait; nothing is lost. |
| **Key refused** | The keys don't match. Paste the node's key (its `NODE_KEY` or `AGENT_TOKEN`, or the one it printed in `docker logs`) into the app's node. |
| **Update needed** | The app and the node are different versions. The message says which side to update. |
| "is next to a different Plex server" | The address points at the node of another Plex. Both sides compare Plex's machine identifier, so markers never go into the wrong database. |
| "Plex's database is on a network share" | The node's mount is a network share. It has to be a local disk on the Plex machine. |
| "Plex doesn't have its database open through this folder right now" | Plex is stopped, or the node has a different path to the file than Plex does. |
| "Plex hasn't created its marker tag yet" | Run Plex's own intro or credits detection once on any item. The node never creates that row. |

`docker logs plex-marker-agent` shows one line per item or stream written. It never logs the key.

## Building it

From the repository root (it needs the app's source, which is what it runs):

```bash
docker build -f nodes/plex-db/Dockerfile -t plex-marker-agent:dev .
```

The image carries no ffmpeg, no GPU drivers and Pillow for validating chapter JPEGs: the app's wheel is installed with
`--no-deps`, and `requirements.txt` holds the handful of libraries it really needs — each line copied from the app's
`pyproject.toml`, with a test that fails if one drifts.

`requirements.txt` is the hand-written input; `requirements.lock.txt` is what the image actually installs. It pins
the whole closure (the transitive libraries too) at one version each with `--generate-hashes`, and the Dockerfile
installs it with `--require-hashes`, so two builds of one tag are the same image rather than whatever PyPI happened
to serve that day. The base image is pinned by digest for the same reason. Regenerate the lock after editing
`requirements.txt` — from the repository root:

```bash
docker run --rm -v "$PWD/nodes/plex-db:/w" -w /w python:3.12-slim-bookworm \
  sh -c 'pip install pip-tools && pip-compile --generate-hashes --strip-extras --no-header \
           --output-file=requirements.lock.txt requirements.txt'
```

CI builds this image, boots it and calls it on every pull request (`marker-agent-image` in `.github/workflows/ci.yml`),
so a new module-scope import on the role's path (`nodes/server/`, `markers/publishers/`, `loudness/plex_db.py`,
`loudness/guard.py`, `library_health/plex_rows.py`) that the `--no-deps` install can't satisfy fails a PR instead of
a release. Releases are cut by pushing a `marker-agent-v<version>` tag, which runs
`.github/workflows/marker-agent.yml`.

**For maintainers:** the published image is still `ghcr.io/stevezau/plex-marker-agent` and the release tags are still
`marker-agent-v*`, so Watchtower and pinned compose files keep finding it. Renaming either (to a `plex-db-node`
image, say) is a release-time decision: it strands every existing install on its last tag unless the old name keeps
being published alongside.

## Versions

The node has its own version (`AGENT_VERSION`, the image tag) and implements one or more numbered **protocols** —
the shape of the requests and answers. The app sends the protocol it speaks on every request and reads the node's
version and protocols off every answer, so a mismatch is caught on the first call, in either direction:

- the node is older than the app's `MIN_AGENT_VERSION` → refused, naming both versions and saying to update the
  node;
- the node has moved past the protocol the app speaks → refused, naming both protocols and saying to update the
  **app** (updating the node again would send you in a circle);
- either way the app shows **Update needed** on the Edit tab and **writes nothing**. Markers wait for the next run.

A new protocol number is only ever *added* to the node, never swapped, so a node can serve an older app while you
update them one at a time. Update the node first: a newer node still speaks the older app's protocol.

An optional field added to a request doesn't change the protocol: an older node ignores it and behaves as before.
The node supports the file limits carried by a write (`limits`, the file's duration). With them
"Keep Plex's" drops a Plex marker that can't be right for the file, such as credits that start after it ends, and
writes ours.

Item reads include `stale_types` (the types whose markers Plex made for
an earlier file at the path, from `taggings.created_at`, `media_parts.updated_at` and the parts' `pv:` records) and
each part's `updated_at`. If a node omits these fields, the app reads that as "can't tell": every Plex marker counts
as before, and its "Keep Plex's" keeps stale markers even where the app has an answer of its own.

`AGENT_VERSION` in `media_preview_generator/nodes/server/plex_db.py` is the one place that version is decided. The image tag in
`docker-compose.yml` and in the `docker run` above are copies of it that a human has to read, so a test
(`TestTheImageStaysInStepWithTheApp` in `tests/nodes/test_plex_db_role.py`) fails when either drifts from it, and the release workflow refuses a
`marker-agent-v<version>` tag whose version isn't the one the code reports.

## The contract

Everything is `POST` with a JSON body, except `/v1/health`, `/v1/ping`, `/v1/info` and `/v1/log`. Every request carries
`Authorization: Bearer <key>` and `X-Marker-Agent-Protocol: 1` (`/v1/info` and `/v1/log` take
`X-MPG-Node-Protocol: 1` instead, or answer 409); every answer but `/v1/log`'s carries
`{"agent": {"version", "protocols", "capabilities"}}`. Protocol 2 is protocol 1 plus `X-MPG-Node-Uid: <the node's id>`,
which the app sends once it has recorded the node's id: a node with another id answers 409
`{"error": {"kind": "wrong_node"}}` and runs nothing, so a machine given another node's key and address never writes
into its Plex what was meant for that node's.

| Endpoint | What it does | Answers |
|---|---|---|
| `GET /v1/health` | Liveness for the container's health check. No key, nothing about your library. | `{"ok": true, "agent": …}` |
| `GET /v1/ping` | The version, advertised capabilities, which Plex this node serves, and whether the database file is there. Needs the key but no protocol header, so it still answers when the two sides disagree. The app checks it before chapter operations; marker "Check again" runs the two checks below. | `agent.capabilities`, `result.db_present`, `result.machine_identifier` |
| `GET /v1/info` | What the app's node checks read: the node's version and roles, its platform, which Plex it serves and whether the database file is there. | `node`, `platform`, `plex_db`, `agent` |
| `GET /v1/log?lines=N` | The node's recent log lines for its card in the app (200 by default, 2000 at most). | `lines` |
| `POST /v1/checks/file` | Is the database found, on a local disk, writable, and open in Plex's process? Also which Plex this is. | `result.report` (a capability report), `result.machine_identifier` |
| `POST /v1/checks/db` | Is it the tested schema, is its marker data readable, is Plex's marker tag row there? | `result.report` |
| `POST /v1/item/read` | One item's live parts (schema and tag row checked first). | `result.item` |
| `POST /v1/item/write` | Write one item's markers, in one transaction. | `result.write` |
| `POST /v1/items/shown` | What items show of what the app left on them (a read-back). | `result.batch` |
| `POST /v1/item/exists` | Does this Plex still have that item? | `result.exists` |
| `POST /v1/chapters/{check,read,register}` | Chapter thumbnails (capability `chapters_v1`, below). | `result.report` / `result.target` / `result.registered` |
| `POST /v1/loudness/{file_checks,check,streams,snapshot,write,mark}` | Loudness (capability `loudness_v1`): the database's checks, a file's audio streams, an item's snapshot before analysis, one stream's write, the item's mark. Every one but `file_checks` carries the identity the app read from Plex (`machine_identifier`, `pms_version`), which must match `Preferences.xml` and a tested Plex version. | `result.report` / `result.streams`, `result.in_plex` / `result.snapshot` / `result.written` / `result.marked` |
| `POST /v1/library-health/rows` | Library Health's fixed counting queries for the named sections (capability `library_health_v1`). | `result.rows` |

Status codes: `200` a result, `400` a body the node can't read, `401` no key or the wrong key (and nothing else —
an unauthenticated caller learns nothing about this Plex), `409` a refusal the app re-raises as its own error (a
network filesystem, an unknown schema, a missing marker tag row, Plex busy), a protocol it doesn't speak, or
`wrong_node`, `413` (plain text) a body over 512 KB, `429` `rate_limited` with `Retry-After`: too many wrong keys
came from that address, so the key isn't checked until then.

The payload shapes live in `media_preview_generator/markers/publishers/plex_remote.py` — one codec, used by both
sides, so they can't disagree about a field.

### Optional capabilities

An answer's `agent.capabilities` lists what this build can do beyond markers: `chapters_v1`, `loudness_v1`,
`library_health_v1`. The app checks for a capability (asking `/v1/ping` when it isn't sure) before it sends one of
those requests, so an older node gets an "update the node" message from the app instead of a request it
can't answer, and nothing is written.

### Chapter capability

`chapters_v1` advertises fixed authenticated `/v1/chapters/check`, `/read`, and `/register` operations.
Chapter writes are verified on Plex Media Server **1.43.4.x and 1.43.5.x**, with matching database schema.
Other versions are refused until verified. Readiness does not require any chapter or marker rows to exist.

Each request requires a positive match between the connected Plex identity and this folder's
`ProcessedMachineIdentifier`. Registration rechecks the exact item, media part, source hash, size, modification
timestamp, chapter rows, and previous thumbnail references inside the same SQLite transaction. All expected
JPEGs must exist and match their submitted SHA-256 digests. The node derives every output path itself.
The node adds content revisions to image URLs, but Plex's normal file checks can remove them.
Plex may continue serving older cached thumbnails after replacement, including after its own native generation;
these revisions do not guarantee cache invalidation. See the [chapter thumbnail cache limitation](../../docs/guides.md#plex-chapter-thumbnails).
Multipart and multiple-version items are refused until their chapter ownership has been verified.
