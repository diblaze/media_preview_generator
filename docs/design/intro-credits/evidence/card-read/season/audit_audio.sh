#!/bin/bash
# The answer key's intro items through the season step in the base tree, then the work tree (niced, sequential: they
# share the pair-run cache).
set -euo pipefail
readonly HERE="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local"
readonly WORK="/home/data/workspace/plex_generate_vid_previews"
cd "$HERE"
export CAP=""
CODE="$HERE/base" nice -n 19 /home/data/.venv/bin/python audit_audio.py "$HERE/ct/audio_base.json" > "$HERE/log_audit_audio_base.log" 2>&1
CODE="$WORK" nice -n 19 /home/data/.venv/bin/python audit_audio.py "$HERE/ct/audio_work.json" > "$HERE/log_audit_audio_work.log" 2>&1
echo ALLDONE >> "$HERE/log_audit_audio_work.log"
