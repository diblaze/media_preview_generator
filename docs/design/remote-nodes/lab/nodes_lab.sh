#!/bin/bash
# Remote-nodes lab on `storage`: an app that sees media at /media, two worker nodes that see the same files at
# /mnt/nas (one NVIDIA, one CPU-only), and a Plex database node beside mlab-plex's config. Everything joins the
# existing `mlab` network; see docs/design/lab-servers.md for mlab-plex itself.
#
#   nodes_lab.sh build <tag>   build the app + plex-db images from this checkout
#   nodes_lab.sh up <tag>      start mlab-plex, the app (config copied from mlab-app-remote's) and the nodes
#   nodes_lab.sh down          stop and remove the lab-nodes containers and their own mlab_nodes_* volumes (never
#                              another mlab_* volume); mlab-plex and mlab-plex-agent stay up, other sessions use them
#
# The app's config is a COPY of mlab_app_remote_config, so the v24 migration runs on a real lab settings file and
# its Plex helper (mlab-plex-agent) turns into a legacy Plex database node. The app writes BIFs into mlab-plex's own
# config (mlab_plex_config at /plexcfg), the folder the Plex database node sits beside.
set -euo pipefail

APP=mlab-nodes-app
GPU_NODE=mlab-nodes-gpu
CPU_NODE=mlab-nodes-cpu
DB_NODE=mlab-nodes-plexdb
APP_VOL=mlab_nodes_app_config
NODE_VOLS=(mlab_nodes_gpu_config mlab_nodes_cpu_config mlab_nodes_plexdb_config)
NODE_KEY_FILE="${NODE_KEY_FILE:-/tmp/claude-1000/mlab-nodes.key}"
TZ_NAME=Australia/Sydney

# The same real files mlab-app-remote mounts, read-only. App path -> host path.
MEDIA=(
  "/media/movies/Up (2009)|/data_16tb/Movies/Up (2009) {tmdb-14160}"
  "/media/movies/Toy Story (1995)|/data_16tb/Movies/Toy Story (1995) {tmdb-862}"
  "/media/tv/South Park (1997)/Season 01|/data_16tb/TV Shows/South Park (1997) {tvdb-75897}/Season 01"
  "/media/tv/Rick and Morty (2013)/Season 01|/data_16tb2/TV Shows/Rick and Morty (2013) {tvdb-275274}/Season 01"
)

mounts() {  # $1 = prefix the container sees instead of /media
  local out=() row app host
  for row in "${MEDIA[@]}"; do
    app="${row%%|*}"; host="${row#*|}"
    out+=(-v "${host}:${1}${app#/media}:ro")
  done
  printf '%s\0' "${out[@]}"
}

cmd="${1:-}"; tag="${2:-}"
case "$cmd" in
  build)
    [ -n "$tag" ] || { echo "usage: $0 build <tag>"; exit 2; }
    docker build -t "plex-previews:${tag}" .
    docker build -f nodes/plex-db/Dockerfile -t "plex-db-node:${tag}" .
    ;;
  up)
    [ -n "$tag" ] || { echo "usage: $0 up <tag>"; exit 2; }
    mkdir -p "$(dirname "$NODE_KEY_FILE")"
    [ -s "$NODE_KEY_FILE" ] || python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$NODE_KEY_FILE"
    chmod 600 "$NODE_KEY_FILE"
    key="$(cat "$NODE_KEY_FILE")"
    docker start mlab-plex mlab-plex-agent >/dev/null
    if ! docker volume inspect "$APP_VOL" >/dev/null 2>&1; then
      docker volume create "$APP_VOL" >/dev/null
      docker run --rm -v mlab_app_remote_config:/from:ro -v "$APP_VOL":/to alpine sh -c 'cp -a /from/. /to/'
    fi
    mapfile -d '' app_mounts < <(mounts /media)
    mapfile -d '' node_mounts < <(mounts /mnt/nas)
    docker run -d --name "$APP" --network mlab -p 127.0.0.1:18090:8080 \
      -e PUID=1000 -e PGID=1000 -e TZ="$TZ_NAME" -v "$APP_VOL":/config -v mlab_plex_config:/plexcfg \
      --runtime=nvidia --gpus all "${app_mounts[@]}" "plex-previews:${tag}"
    docker run -d --name "$GPU_NODE" --network mlab -e MODE=node -e NODE_NAME=lab-gpu -e NODE_KEY="$key" -e TZ="$TZ_NAME" -v mlab_nodes_gpu_config:/config \
      -e PUID=1000 -e PGID=1000 --runtime=nvidia --gpus all "${node_mounts[@]}" "plex-previews:${tag}"
    docker run -d --name "$CPU_NODE" --network mlab -e MODE=node -e NODE_NAME=lab-cpu -e NODE_KEY="$key" -e TZ="$TZ_NAME" -v mlab_nodes_cpu_config:/config \
      -e PUID=1000 -e PGID=1000 "${node_mounts[@]}" "plex-previews:${tag}"
    # Plex in mlab-plex runs as 1000:1000, so the database node does too.
    docker run -d --name "$DB_NODE" --network mlab --user 1000:1000 -e NODE_KEY="$key" -e TZ="$TZ_NAME" \
      -e PLEX_CONFIG_DIR="/plexcfg/Library/Application Support/Plex Media Server" \
      -v mlab_plex_config:/plexcfg -v mlab_nodes_plexdb_config:/config "plex-db-node:${tag}"
    echo "app: http://127.0.0.1:18090   nodes: http://${GPU_NODE}:9494 http://${CPU_NODE}:9494 http://${DB_NODE}:9494"
    echo "node key file: ${NODE_KEY_FILE}"
    ;;
  down)
    docker rm -f "$APP" "$GPU_NODE" "$CPU_NODE" "$DB_NODE" 2>/dev/null || true
    docker volume rm "$APP_VOL" "${NODE_VOLS[@]}" 2>/dev/null || true
    ;;
  *) echo "usage: $0 build|up|down [tag]"; exit 2 ;;
esac
