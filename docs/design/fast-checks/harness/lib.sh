#!/usr/bin/env bash
# Shared helpers for the fast-checks comparison harness. Source this file; do not run it.
#
# Everything the harness creates lives under $BASE (snapshots, per-run scratch, results). Docker objects it
# creates are named mlab-fastcheck-*. The lab containers and mlab_* volumes are only stopped, started, restored
# (mlab_plex_config's database and Media/localhost) or read; never removed.
set -euo pipefail

HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE="${FASTCHECK_BASE:-/home/data/mlab-fastcheck}"
SNAP="$BASE/snapshots"
RUNS="$BASE/runs"
OUT="${FASTCHECK_OUT:-$BASE/out}"
PLEX=mlab-plex
PLEX_VOL=mlab_plex_config
PLEX_URL="http://127.0.0.1:32402"
APP_SRC_VOL=mlab_app_config_captions
APP_PORT="${FASTCHECK_APP_PORT:-18090}"
WORKERS="${FASTCHECK_WORKERS:-8}"
HELPER_IMAGE=alpine:latest
PLEX_ROOT="/p/Library/Application Support/Plex Media Server"
PYTHON="${PYTHON:-python3}"

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Run a shell snippet as root in a throwaway container that sees the Plex config volume at /p, the snapshots at /snap
# and the run scratch at /runs.
helper() {
    docker run --rm --name "mlab-fastcheck-helper-$$-$RANDOM" \
        -v "$PLEX_VOL":/p -v "$SNAP":/snap -v "$RUNS":/runs "$HELPER_IMAGE" sh -c "$1"
}

plex_stop() {
    log "stopping $PLEX"
    docker stop -t 90 "$PLEX" >/dev/null 2>&1 || true
}

plex_start() {
    log "starting $PLEX"
    docker start "$PLEX" >/dev/null
    local i
    for i in $(seq 1 120); do
        if [ "$(docker inspect -f '{{.State.Health.Status}}' "$PLEX" 2>/dev/null)" = healthy ] \
            && curl -fs -m 3 "$PLEX_URL/identity" >/dev/null 2>&1; then
            sleep 5
            return 0
        fi
        sleep 3
    done
    die "$PLEX did not become healthy"
}

# plex_snapshot NAME: Plex stopped. Copies the library database (+ blobs) and Media/localhost.
plex_snapshot() {
    local name="$1"
    mkdir -p "$SNAP/$name" "$RUNS"
    helper "set -e
        D=\"$PLEX_ROOT/Plug-in Support/Databases\"
        rm -rf /snap/$name/plex; mkdir -p /snap/$name/plex/Databases
        for f in com.plexapp.plugins.library.db com.plexapp.plugins.library.db-wal com.plexapp.plugins.library.blobs.db com.plexapp.plugins.library.blobs.db-wal; do
            [ -e \"\$D/\$f\" ] && cp -a \"\$D/\$f\" /snap/$name/plex/Databases/
        done
        cp -a \"$PLEX_ROOT/Media/localhost\" /snap/$name/plex/localhost
        chown -R 1000:1000 /snap/$name/plex"
}

# plex_restore NAME: Plex stopped. Puts the snapshot's database and Media/localhost back.
plex_restore() {
    local name="$1"
    [ -s "$SNAP/$name/plex/Databases/com.plexapp.plugins.library.db" ] || die "snapshot $name has no Plex database"
    [ -d "$SNAP/$name/plex/localhost" ] || die "snapshot $name has no Media/localhost"
    helper "set -e
        D=\"$PLEX_ROOT/Plug-in Support/Databases\"
        M=\"$PLEX_ROOT/Media\"
        for f in com.plexapp.plugins.library.db com.plexapp.plugins.library.db-shm com.plexapp.plugins.library.db-wal com.plexapp.plugins.library.blobs.db com.plexapp.plugins.library.blobs.db-shm com.plexapp.plugins.library.blobs.db-wal; do
            rm -f \"\$D/\$f\"
        done
        cp -a /snap/$name/plex/Databases/. \"\$D\"/
        rm -rf \"\$M/localhost\"
        cp -a /snap/$name/plex/localhost \"\$M/localhost\""
}

# plex_copy_out DIR: Plex stopped. Copies the current database and Media/localhost to $RUNS/DIR/plex for collect.py.
plex_copy_out() {
    local dir="$1"
    helper "set -e
        D=\"$PLEX_ROOT/Plug-in Support/Databases\"
        rm -rf /runs/$dir/plex; mkdir -p /runs/$dir/plex/Databases
        for f in com.plexapp.plugins.library.db com.plexapp.plugins.library.db-wal; do
            [ -e \"\$D/\$f\" ] && cp -a \"\$D/\$f\" /runs/$dir/plex/Databases/
        done
        cp -a \"$PLEX_ROOT/Media/localhost\" /runs/$dir/plex/localhost
        chown -R 1000:1000 /runs/$dir/plex"
}

# app_snapshot CFGDIR NAME: copies an app config dir to the snapshot, leaving out per-run logs and job history.
app_snapshot() {
    local cfg="$1" name="$2"
    rm -rf "${SNAP:?}/$name/app"
    mkdir -p "$SNAP/$name/app"
    tar -C "$cfg" --exclude=./logs --exclude='./jobs.db*' --exclude='./scheduler.db*' --exclude='./webhook_history*' \
        --exclude='*.bak' --exclude=./.cache -cf - . | tar -C "$SNAP/$name/app" -xf -
}

# app_mounts: the real-media :ro bind mounts of the lab app, one "-v" argument pair per line.
app_mounts() {
    docker inspect mlab-app | "$PYTHON" -c '
import json, sys
for m in json.load(sys.stdin)[0]["Mounts"]:
    if m["Type"] == "bind" and m["Destination"].startswith("/media/") and "/evidence/lab/synth/" not in m["Source"]:
        print(m["Source"] + ":" + m["Destination"] + ":ro")'
}

# app_start IMAGE CFGDIR TAG: starts a throwaway app container on the lab network; prints nothing.
app_start() {
    local image="$1" cfg="$2" tag="$3" name="mlab-fastcheck-app-$3"
    local -a mounts=()
    local line
    while IFS= read -r line; do mounts+=(-v "$line"); done < <(app_mounts)
    [ "${#mounts[@]}" -gt 0 ] || die "no media mounts found on mlab-app"
    docker rm -f "$name" >/dev/null 2>&1 || true
    docker run -d --name "$name" --network mlab \
        -e WEB_AUTH_TOKEN="$FASTCHECK_TOKEN" -e PUID=1000 -e PGID=1000 -e TZ=UTC \
        -v "$cfg":/config -v "$PLEX_VOL":/plexcfg \
        -p "127.0.0.1:$APP_PORT:8080" "${mounts[@]}" "$image" >/dev/null
}

app_remove() {
    local name="mlab-fastcheck-app-$1"
    docker stop -t 60 "$name" >/dev/null 2>&1 || true
    docker rm -f -v "$name" >/dev/null 2>&1 || true
}

new_token() { "$PYTHON" -c 'import secrets; print(secrets.token_hex(16))'; }

# run_on_snapshot IMAGE STATE TAG: restore STATE, start the app from IMAGE, run the fixed job sequence, stop the app.
# Leaves Plex stopped, the app's config at $RUNS/TAG/cfg and the job summary at $RUNS/TAG/jobs.json.
run_on_snapshot() {
    local image="$1" state="$2" tag="$3"
    local cfg="$RUNS/$tag/cfg"
    rm -rf "${RUNS:?}/$tag"
    mkdir -p "$cfg"
    plex_stop
    plex_restore "$state"
    plex_start
    cp -a "$SNAP/$state/app/." "$cfg/"
    export FASTCHECK_TOKEN
    FASTCHECK_TOKEN="$(new_token)"
    log "[$tag] starting app from $image on snapshot '$state'"
    app_start "$image" "$cfg" "$tag"
    FASTCHECK_APP_URL="http://127.0.0.1:$APP_PORT" "$PYTHON" "$HARNESS/run_jobs.py" "$RUNS/$tag/jobs.json" \
        || { docker logs --tail 40 "mlab-fastcheck-app-$tag" >&2 || true; app_remove "$tag"; die "job run failed ($tag)"; }
    app_remove "$tag"
    plex_stop
}
