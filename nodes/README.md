# Nodes

A **node** is another machine that helps the app. The app stays in charge: it keeps the queue, the schedules, your
server tokens and every write. A node runs one heavy step it is handed and sends the result back.

A node has one or both roles:

* **Worker**: lends the app its GPU (or CPU) for previews, loudness and intro/credits detection. Run it on any
  machine that can read your media.
* **Plex database**: opens Plex's database on the machine Plex runs on, so the app can write intro/credits markers
  and loudness into Plex from elsewhere. This is the former Plex marker agent.

## Worker node

The app's own image, started with `MODE=node`. See [`worker/docker-compose.yml`](worker/docker-compose.yml).

* Run the **same image tag** as the app: a worker node must be on the app's exact version.
* Mount your media **read-only**, at any path. The app maps its paths to the node's (Settings → Nodes → path
  mappings) and checks each file is the same one (size and modification time) before using it.
* Pass the GPU exactly as you would to the app: `--device /dev/dri:/dev/dri` for Intel/AMD, or
  `--gpus all` / `runtime: nvidia` with `NVIDIA_DRIVER_CAPABILITIES=all` for NVIDIA. Leave `/dev/dri` out on a machine
  without an Intel or AMD GPU: the container doesn't start without it ("error gathering device information").
* The node makes its own key on first start and prints it once in `docker logs`. It is kept in
  `/config/node.json`, so mount `/config`. To pick your own, add `-e NODE_KEY=...` with a key made by
  `openssl rand -hex 24` (at least 16 characters). To keep it in a file (a Docker secret), set `FILE__NODE_KEY`
  to that file's path instead; any of the node's settings works this way, but not `MODE`, `PUID`, `PGID` or `TZ`,
  which the container reads before the node starts.
* `NODE_NAME` is the name the app shows (default: the hostname). The node listens on port **9494** (`NODE_PORT`;
  change the published port, `-p`, to match).

```bash
docker run -d --name media-preview-node -p 9494:9494 \
  -e MODE=node -e PUID=1000 -e PGID=1000 \
  --device /dev/dri:/dev/dri \
  -v /path/to/media:/media:ro -v /path/to/node/config:/config \
  stevezzau/media_preview_generator:latest
```

### Updating it

Update every worker node to the app's tag whenever you update the app: until then it says **Update needed** and gets
no work. `docker compose pull && docker compose up -d` (or `docker pull` and the same `docker run` again) keeps its
`/config`, so its id and key stay. Watchtower can update nodes too.

## Plex database node

Runs beside Plex, with Plex's config folder mounted. Use the slim Linux image in [`plex-db/`](plex-db/) or the
Windows build in [`windows/`](windows/). It doesn't have to match the app's version, only be new enough: the app says
when it needs an update.

## Add it in the app

Settings → **Nodes** → **Add node**: the node's address (`http://machine:9494`) and its key. The app checks the
node, then asks for the path mappings and tests that the node can read a few files from each library.
