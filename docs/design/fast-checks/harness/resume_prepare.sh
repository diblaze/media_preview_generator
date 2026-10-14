#!/usr/bin/env bash
# resume_prepare.sh A_IMAGE
#
# Finishes a prepare.sh that stopped after its Intro & Credits pass: lab Plex and $RUNS/prepare/cfg still hold that
# pass's state. Runs the remaining jobs with A_IMAGE, then snapshots steady, makes mutated and puts lab Plex back.
. "$(dirname "$0")/lib.sh"

image="${1:?usage: resume_prepare.sh A_IMAGE}"
cfg="$RUNS/prepare/cfg"
[ -s "$cfg/settings.json" ] || die "no prepare config at $cfg"

plex_start
export FASTCHECK_TOKEN
FASTCHECK_TOKEN="$(new_token)"
log "[prepare] resuming with $image"
app_start "$image" "$cfg" prepare
FASTCHECK_JOBS=loudness,previews FASTCHECK_APP_URL="http://127.0.0.1:$APP_PORT" \
    "$PYTHON" "$HARNESS/run_jobs.py" "$RUNS/prepare/jobs-rest.json" \
    || { docker logs --tail 40 mlab-fastcheck-app-prepare >&2 || true; app_remove prepare; die "job run failed"; }
app_remove prepare
plex_stop
plex_snapshot steady
app_snapshot "$cfg" steady
mkdir -p "$BASE/prepare-logs"
cp -a "$RUNS/prepare/jobs-rest.json" "$BASE/prepare-logs/" 2>/dev/null || true
cp -a "$cfg/logs" "$BASE/prepare-logs/" 2>/dev/null || true

log "making mutated"
"$PYTHON" "$HARNESS/mutate.py" "$SNAP" steady mutated

rm -rf "${RUNS:?}/prepare" "${RUNS:?}/basecfg"
plex_restore orig
plex_start
du -sh "$SNAP"/* | sed 's#^#snapshot size: #' >&2
log "prepare done"
