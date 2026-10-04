#!/bin/bash
# Usage: run.sh base|work TAG SETS — one tree's credit text on the sets, niced, log in logs/.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly BASE_TREE="$HERE/base"
readonly WORK_TREE="${CAPTIONS_WORK:-$(cd "$HERE/../../../../.." && pwd)}"
which="$1"; tag="$2"; sets="$3"
mkdir -p "$HERE/logs"
if [[ "$which" == "base" ]]; then tree="$BASE_TREE"; else tree="$WORK_TREE"; fi
nice -n 19 /home/data/.venv/bin/python "$HERE/ctrun.py" "$tree" "$HERE/ct_${tag}.json" "$sets" "${@:4}" \
    >> "$HERE/logs/ct_${tag}.log" 2>&1
echo "DONE $sets" >> "$HERE/logs/ct_${tag}.log"
