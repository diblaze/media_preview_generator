#!/bin/bash
# Usage: srun.sh base|new LABEL CAP script args...  (CAP "" = the tree's own)
set -euo pipefail
readonly HERE="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local"
tree="$1"; label="$2"; cap="$3"; shift 3
if [[ "$tree" == "base" ]]; then
    export CODE="$HERE/base"
else
    export CODE="/home/data/workspace/plex_generate_vid_previews"
fi
export CAP="$cap"
cd "$HERE"
exec nice -n 19 /home/data/.venv/bin/python "$@" > "$HERE/log_${label}.log" 2>&1
