#!/usr/bin/env bash
# compare.sh A_IMAGE B_IMAGE
#
# For each image (old A, new B) and each start state (steady, mutated): restore the snapshot, run the same jobs, collect
# what they did into $OUT/{A,B}/<state>/. Then diff.py prints every difference and the timing. Needs `prepare.sh` first.
# Exit status 1 when there is any difference. Lab Plex is put back to its original state (`orig`) and left running;
# set FASTCHECK_LEAVE_PLEX=1 to skip the put-back.
. "$(dirname "$0")/lib.sh"

image_a="${1:?usage: compare.sh A_IMAGE B_IMAGE}"
image_b="${2:?}"
for state in steady mutated; do
    [ -d "$SNAP/$state" ] || die "snapshot $state missing: run prepare.sh first"
done

started=$(date +%s)
rm -rf "${OUT:?}/A" "${OUT:?}/B"
cleanup() {
    docker ps -a --format '{{.Names}}' | grep '^mlab-fastcheck-app-' | while read -r c; do docker rm -f -v "$c" >/dev/null 2>&1 || true; done
    if [ "${FASTCHECK_LEAVE_PLEX:-0}" != 1 ] && [ -d "$SNAP/orig/plex" ]; then
        plex_stop
        plex_restore orig
    fi
    plex_start || true
}
trap cleanup EXIT

for state in steady mutated; do
    for side in A B; do
        image="$image_a"
        [ "$side" = B ] && image="$image_b"
        log "== $side ($image) from $state"
        "$HARNESS/run_state.sh" "$image" "$state" "$OUT/$side/$state"
    done
done

log "diffing"
status=0
"$PYTHON" "$HARNESS/diff.py" "$OUT/A" "$OUT/B" || status=$?
log "compare took $(( $(date +%s) - started )) s"
exit "$status"
