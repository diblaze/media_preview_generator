#!/bin/bash
# The library chapter set in full for each cap (every change printed against the base; no stop).
set -euo pipefail
readonly HERE="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local"
for cap in "$@"; do
    "$HERE/srun_cmp.sh" new "w_c${cap}_chap" "$cap" sruns.py "w_c${cap}_chap" chap_clean || true
done
echo ALLDONE >> "$HERE/log_season_chap.log"
