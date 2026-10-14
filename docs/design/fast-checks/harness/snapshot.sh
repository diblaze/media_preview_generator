#!/usr/bin/env bash
# snapshot.sh NAME [--app-dir DIR] [--keep-stopped]
#
# Snapshot lab Plex (library database + blobs + Media/localhost) into $BASE/snapshots/NAME/plex. Plex is stopped while
# it is copied, then started again (unless --keep-stopped). With --app-dir, also snapshot that app config directory
# (settings.json, markers.db, loudness.db...; not logs or job history) into NAME/app.
#
# What is left out on purpose: Plex's Metadata/ (851 MB of posters and art, unused by these jobs), Cache, Logs and its
# dated database backups. Sizes are printed so a change in what a snapshot holds is visible.
. "$(dirname "$0")/lib.sh"

name="${1:?usage: snapshot.sh NAME [--app-dir DIR] [--keep-stopped]}"
shift
app_dir=""
keep_stopped=0
while [ $# -gt 0 ]; do
    case "$1" in
        --app-dir) app_dir="$2"; shift 2 ;;
        --keep-stopped) keep_stopped=1; shift ;;
        *) die "unknown option $1" ;;
    esac
done

mkdir -p "$SNAP" "$RUNS"
plex_stop
plex_snapshot "$name"
[ -z "$app_dir" ] || app_snapshot "$app_dir" "$name"
[ "$keep_stopped" = 1 ] || plex_start
du -sh "$SNAP/$name"/* | sed 's#^#snapshot size: #' >&2
