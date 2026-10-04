#!/bin/bash
# The final work tree on everything, from the cached decodes: ILA + Killer Cases (own digest), then the sets and the
# library (the v8 lane's digest).
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly DIGEST=1614b22dd1ed9f23
"$HERE/run.sh" work final ila,kc
"$HERE/run.sh" work finalsets 80,items,accused,isurvived,205,more --decode-digest "$DIGEST"
echo FINAL_DONE
