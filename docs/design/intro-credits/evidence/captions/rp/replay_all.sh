#!/bin/bash
# Final replay: base and work trees on the final audit's markers.db copy, each with its credit text on every file read.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PY=/home/data/.venv/bin/python
readonly DB="$HERE/../../final-audit/db/markers.db"
export CREDFIX_BASE="$HERE/../base"
export CREDFIX_WORK="${CAPTIONS_WORK:-$(cd "$HERE/../../../../../.." && pwd)}"
cd "$HERE"
"$PY" - <<'EOF'
import json
b = json.load(open("../ct_ball.json")); b.update(json.load(open("../ct_base.json")))
w = json.load(open("../ct_finalsets.json")); w.update(json.load(open("../ct_final.json")))
b = {k: v for k, v in b.items() if k in w}
json.dump(b, open("text_base.json", "w")); json.dump(w, open("text_work.json", "w")); print(len(b), len(w))
EOF
nice -n 19 "$PY" replay.py base replay_base.json --text text_base.json --db "$DB" 2>&1 | grep -v DEBUG | tail -1
nice -n 19 "$PY" replay.py work replay_work.json --text text_work.json --db "$DB" 2>&1 | grep -v DEBUG | tail -1
"$PY" diff_replay.py replay_base.json replay_work.json > diff.txt
tail -1 diff.txt
"$PY" metrics.py replay_base.json replay_work.json
"$PY" pos_eval.py replay_base.json replay_work.json
