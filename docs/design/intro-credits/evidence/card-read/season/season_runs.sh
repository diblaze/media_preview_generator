#!/bin/bash
# The new tree's season step, cheapest sets first, per cap; each variant stops at its first worse verdict (fail fast).
set -euo pipefail
readonly HERE="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local"
for cap in "$@"; do
    "$HERE/srun_ff.sh" new "w_c${cap}_all" "$cap" sruns.py "w_c${cap}_all" lists scale_clean sample accused chap_clean \
        || echo "variant cap ${cap} stopped (exit $?)" >> "$HERE/log_w_c${cap}_all.log"
done
echo ALLDONE >> "$HERE/log_season_runs.log"
