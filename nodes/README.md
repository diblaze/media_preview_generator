# Nodes

A **node** is another machine that does work for the app. The app keeps the queue, the schedules, your server tokens
and every write. A node runs the heavy step it is handed and sends the result back.

There are two kinds:

| Kind | Use it to | Runs on |
|---|---|---|
| **Worker node** | Lend a GPU or CPU for previews, Intro & Credits detection and loudness. | Any machine that can read your media. |
| **Plex database node** | Get markers, chapter thumbnails, loudness and Library Health counts into a Plex that runs on another machine. | The machine Plex runs on. |

The Plex database node was called the Plex marker agent before 5.2.0. An existing install keeps working.

Overview with screenshots: [Remote nodes](https://mediapreviewgenerator.dev/remote-nodes/).

## Worker node

A worker node is the app's own image started with `MODE=node`.

You need:

- Docker on the other machine.
- Read access to your media from that machine.
- The same image tag as the app. A worker node must run the app's exact version.

Steps:

1. Start the node. Pass the GPU the way you pass it to the app.

   ```bash
   docker run -d --name media-preview-node -p 9494:9494 \
     -e MODE=node -e NODE_NAME=gpu-box -e PUID=1000 -e PGID=1000 \
     --device /dev/dri:/dev/dri \
     -v /path/to/media:/media:ro -v /path/to/node/config:/config \
     stevezzau/media_preview_generator:latest
   ```

   - Intel or AMD: `--device /dev/dri:/dev/dri`. Leave it out on a machine without one, or the container won't start
     ("error gathering device information").
   - NVIDIA: `--gpus all -e NVIDIA_DRIVER_CAPABILITIES=all` instead.
   - Prefer Compose? Use [`worker/docker-compose.yml`](worker/docker-compose.yml).
2. Read the key from the log: `docker logs media-preview-node`. It is printed once, on first start, and kept in
   `/config/node.json`. Keep `/config` mounted so the key and the node's id survive updates.
3. In the app, open **Settings → Nodes → Add node**. Enter a name, the node's address (`http://gpu-box:9494`) and
   the key, then click **Connect**.
4. Click **Next: where it sees your media**. Map each library folder to the folder the node sees, click
   **Test access**, then **Save**. If the media is mounted at the same path as in the app, the mappings it fills in
   are already right; at a different path, edit them. You can change them later on the node's **Media** tab.
5. In **Settings → Workers**, add the node's GPU or CPU to a worker group.

Settings:

| Variable | What it does |
|---|---|
| `MODE=node` | Starts the node instead of the app. |
| `NODE_NAME` | The name the app shows. Default: the hostname. |
| `NODE_PORT` | The port it listens on inside the container (default `9494`). Change the published port (`-p`) to match. |
| `NODE_KEY` | Your own key instead of a generated one: 16 to 200 printable characters, no spaces. Make one with `openssl rand -hex 24`. |
| `FILE__NODE_KEY` | The path of a file holding the key, such as a Docker secret. Any node setting works this way except `MODE`, `PUID`, `PGID` and `TZ`. |

All node settings are in the [reference](https://mediapreviewgenerator.dev/reference/#nodes).

### Update a worker node

Update the node to the app's tag whenever you update the app. Until then the node says **Update needed** and gets no
work.

1. Use the new tag in your compose file or `docker run` command.
2. Compose: `docker compose pull && docker compose up -d`. Plain Docker: `docker pull`, then
   `docker stop media-preview-node && docker rm media-preview-node`, then the same `docker run` again.

`/config` stays, so the node keeps its id and key. Watchtower can update nodes like it updates the app.

### Remove a worker node

Take the node out of every worker group. Then open it under **Settings → Nodes** and click **Remove** on its
**Settings** tab. The node keeps running until you stop it:
`docker stop media-preview-node && docker rm media-preview-node` on its machine.

## Plex database node

Run it beside Plex, with Plex's config folder mounted.

- **Plex in Docker, or on Linux, Unraid or a NAS:** use the container in [`plex-db/`](plex-db/).
- **Plex installed on Windows:** use the Windows program in [`windows/`](windows/).

It doesn't have to match the app's version, only be new enough. The app says when it needs an update.

Then add it in the app: **Settings → Nodes → Add node**, with its address and key. After that, choose it for your
Plex server under **Servers → your Plex → Edit → Connection → Plex database**.
