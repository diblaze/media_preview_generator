#!/usr/bin/env bash
# build_image.sh COMMIT TAG
# Builds the app image from a commit's own files (git archive), never from the working tree.
set -euo pipefail
commit="${1:?usage: build_image.sh COMMIT TAG}"
tag="${2:?}"
repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
dir="$(mktemp -d)"
trap 'rm -rf "$dir"' EXIT
git -C "$repo" archive "$commit" | tar -x -C "$dir"
docker build --build-arg SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+fastcheck -t "$tag" "$dir"
