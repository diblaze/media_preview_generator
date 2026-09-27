#!/bin/bash
# The worktree's season step (app grouping) on the four sets, then the sample.
set -euo pipefail
readonly HERE="/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/fix3"
export GROUPING=app
"$HERE/run.sh" new run_sets.py app accused lists scale_clean chap_clean > "$HERE/sets_app.log" 2>&1
"$HERE/run.sh" new sample_run.py app app > "$HERE/sample_app.log" 2>&1
echo done
