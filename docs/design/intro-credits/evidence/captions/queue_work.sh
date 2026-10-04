#!/bin/bash
# Work tree on ILA + Killer Cases (their own decodes) and the harness sets (the v8 lane's decodes). Usage: queue_work.sh TAG
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DIGEST=1614b22dd1ed9f23
tag="${1:-work}"
"$HERE/run.sh" work "$tag" ila,kc
for sets in 80 items accused isurvived 205; do
    "$HERE/run.sh" work "${tag}sets" "$sets" --decode-digest "$DIGEST"
done
echo ALLDONE >> "$HERE/logs/ct_${tag}sets.log"
