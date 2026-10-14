#!/usr/bin/env bash
# run_state.sh IMAGE STATE OUT_DIR
#
# Restore snapshot STATE, run the fixed job sequence with IMAGE, and write the comparable results to OUT_DIR.
# Lab Plex is left stopped (compare.sh starts it again at the end).
. "$(dirname "$0")/lib.sh"

image="${1:?usage: run_state.sh IMAGE STATE OUT_DIR}"
state="${2:?}"
out_dir="${3:?}"
tag="run-$(basename "$out_dir")-$state-$$"

run_on_snapshot "$image" "$state" "$tag"
plex_copy_out "$tag"
rm -rf "$out_dir"
mkdir -p "$out_dir"
"$PYTHON" "$HARNESS/collect.py" "$RUNS/$tag" "$out_dir"
if [ "${FASTCHECK_KEEP_RUNS:-0}" = 1 ]; then
    mkdir -p "$out_dir/raw-logs" && cp -a "$RUNS/$tag/cfg/logs" "$out_dir/raw-logs/" || true
fi
rm -rf "${RUNS:?}/$tag"
