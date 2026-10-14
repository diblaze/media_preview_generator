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
  `--gpus all` / `runtime: nvidia` with `NVIDIA_DRIVER_CAPABILITIES=all` for NVIDIA.
* Set `NODE_KEY` to a long random key, or leave it out: on first start the node makes one and prints it once in
  `docker logs`. It is kept in `/config/node.json`, so mount `/config`.
* `NODE_NAME` is the name the app shows (default: the hostname). The node listens on port **9494** (`NODE_PORT`).

```bash
docker run -d --name media-preview-node -p 9494:9494 \
  -e MODE=node -e NODE_KEY=change-me -e PUID=1000 -e PGID=1000 \
  --device /dev/dri:/dev/dri \
  -v /path/to/media:/media:ro -v /path/to/node/config:/config \
  stevezzau/media_preview_generator:latest
```

## Plex database node

Runs beside Plex, with Plex's config folder mounted. Use the slim Linux image in [`plex-db/`](plex-db/) or the
Windows build in [`windows/`](windows/).

## Add it in the app

Settings → **Nodes** → **Add node**: the node's address (`http://machine:9494`) and its key. The app checks the
node, then asks for the path mappings and tests that the node can read a few files from each library.
