#!/bin/bash
# Usage: run.sh [base|new] script args... — runs a scratch script against a code tree, niced.
set -euo pipefail
readonly HERE="/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/fix3"
tree="$1"; shift
if [[ "$tree" == "base" ]]; then
    export CODE="$HERE/base"
else
    export CODE="/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a5303f33d82956a43"
fi
cd "$HERE"
exec nice -n 19 /home/data/.venv/bin/python "$@"
