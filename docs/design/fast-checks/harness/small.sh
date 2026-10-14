#!/usr/bin/env bash
# small.sh IMAGE CFG_DIR
#
# Small damage test on lab Plex as it is now and an app config dir (both reused as they are):
#   1. run every job type on ~14 files twice (the first run finishes them, the second must find them all up to date);
#   2. stop everything and damage some files' outputs (small.py damage);
#   3. run again: exactly the damaged files must be redone, every other file up to date (small.py check).
# Lab Plex is put back to the `orig` snapshot at the end.
. "$(dirname "$0")/lib.sh"

image="${1:?usage: small.sh IMAGE CFG_DIR}"
cfg="${2:?}"
work="$BASE/small"
mkdir -p "$work"
export FASTCHECK_CFG="$cfg" FASTCHECK_APP_URL="http://127.0.0.1:$APP_PORT" FASTCHECK_TOKEN
FASTCHECK_TOKEN="$(new_token)"

finish() {
    app_remove small
    plex_stop
    plex_restore orig
    plex_start || true
    log "lab Plex put back to orig"
}
trap finish EXIT

copy_plex_db_out() {
    helper "set -e
        D=\"$PLEX_ROOT/Plug-in Support/Databases\"
        rm -rf /runs/small-db; mkdir -p /runs/small-db
        cp -a \"\$D/com.plexapp.plugins.library.db\" /runs/small-db/
        [ -e \"\$D/com.plexapp.plugins.library.db-wal\" ] && cp -a \"\$D/com.plexapp.plugins.library.db-wal\" /runs/small-db/ || true
        chown -R 1000:1000 /runs/small-db"
}

plex_start
copy_plex_db_out
export FASTCHECK_PLEXDB="$RUNS/small-db/com.plexapp.plugins.library.db"

app_start "$image" "$cfg" small
log "first run (finishes the files)"
"$PYTHON" "$HARNESS/small.py" run "$work/first.json"
log "baseline run (all must be up to date)"
"$PYTHON" "$HARNESS/small.py" run "$work/baseline.json"
app_remove small
plex_stop

log "damaging"
copy_plex_db_out
"$PYTHON" "$HARNESS/small.py" damage "$FASTCHECK_PLEXDB" "$cfg/markers.db" "$work/plan.json"
bif=$("$PYTHON" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["bundles"]["bif"]))' "$work/plan.json")
chapters=$("$PYTHON" -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["bundles"]["chapters"]))' "$work/plan.json")
helper "set -e
    D=\"$PLEX_ROOT/Plug-in Support/Databases\"
    M=\"$PLEX_ROOT/Media/localhost\"
    rm -f \"\$D/com.plexapp.plugins.library.db-wal\" \"\$D/com.plexapp.plugins.library.db-shm\"
    cp /runs/small-db/com.plexapp.plugins.library.db \"\$D/com.plexapp.plugins.library.db\"
    chown 1000:1000 \"\$D/com.plexapp.plugins.library.db\"
    for h in $bif; do d=\"\$M/\$(echo \$h | cut -c1)/\$(echo \$h | cut -c2-).bundle/Contents/Indexes\"; rm -f \"\$d/index-sd.bif\" \"\$d/index-sd.bif.meta\"; done
    for h in $chapters; do d=\"\$M/\$(echo \$h | cut -c1)/\$(echo \$h | cut -c2-).bundle/Contents/Chapters\"; rm -f \"\$d\"/*.jpg; done"

plex_start
app_start "$image" "$cfg" small
log "run after damage"
"$PYTHON" "$HARNESS/small.py" run "$work/after.json"
"$PYTHON" "$HARNESS/small.py" check "$work/baseline.json" "$work/after.json" "$work/plan.json"
