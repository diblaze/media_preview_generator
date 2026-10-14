#!/usr/bin/env bash
# prepare.sh A_IMAGE   (run once; hours the first time)
#
# Builds the starting states every comparison uses:
#   orig     lab Plex exactly as found (so it can be put back)
#   base     orig + a fresh app config (fixed settings, empty markers.db / loudness.db)
#   steady   base after A_IMAGE ran previews, Intro & Credits and loudness on tv + movies to completion
#   mutated  steady with ~5% of files' state damaged (see mutate.py); media untouched
# Lab Plex is put back to `orig` and left running at the end.
. "$(dirname "$0")/lib.sh"

image="${1:?usage: prepare.sh A_IMAGE}"
mkdir -p "$SNAP" "$RUNS"

if [ ! -d "$SNAP/orig/plex" ] || [ "${FASTCHECK_RESNAP_ORIG:-0}" = 1 ]; then
    "$HARNESS/snapshot.sh" orig
fi

log "building base snapshot"
rm -rf "${RUNS:?}/basecfg" "${SNAP:?}/base"
mkdir -p "$RUNS/basecfg" "$SNAP/base"
cp -a "$SNAP/orig/plex" "$SNAP/base/plex"
docker run --rm --name "mlab-fastcheck-helper-$$" -v "$APP_SRC_VOL":/c:ro -v "$RUNS":/runs "$HELPER_IMAGE" \
    sh -c 'cp /c/settings.json /runs/basecfg/settings.raw && chown 1000:1000 /runs/basecfg/settings.raw'
"$PYTHON" "$HARNESS/prep_settings.py" "$RUNS/basecfg/settings.raw" "$RUNS/basecfg/settings.json" "$WORKERS"
rm -f "$RUNS/basecfg/settings.raw"
app_snapshot "$RUNS/basecfg" base

log "running the first pass with $image to build steady"
run_on_snapshot "$image" base prepare
plex_snapshot steady
app_snapshot "$RUNS/prepare/cfg" steady
mkdir -p "$BASE/prepare-logs"
cp -a "$RUNS/prepare/jobs.json" "$BASE/prepare-logs/jobs.json"
cp -a "$RUNS/prepare/cfg/logs" "$BASE/prepare-logs/" 2>/dev/null || true

log "making mutated"
"$PYTHON" "$HARNESS/mutate.py" "$SNAP" steady mutated

rm -rf "${RUNS:?}/prepare" "${RUNS:?}/basecfg"
plex_restore orig
plex_start
du -sh "$SNAP"/* | sed 's#^#snapshot size: #' >&2
log "prepare done"
