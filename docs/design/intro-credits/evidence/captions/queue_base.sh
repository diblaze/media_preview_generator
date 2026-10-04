#!/bin/bash
# Base tree on the harness sets then the rest of sflix's credit-text files, reusing the v8 lane's decodes.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DIGEST=1614b22dd1ed9f23
for sets in 80 items accused isurvived 205 more; do
    "$HERE/run.sh" base bsets "$sets" --decode-digest "$DIGEST"
done
echo ALLDONE >> "$HERE/logs/ct_bsets.log"
