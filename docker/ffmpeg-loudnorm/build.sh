#!/usr/bin/env bash
# Builds /out/ffmpeg (+ /out/jellyfin-release) for loudness analysis; why: see the Dockerfile stage ffmpeg-loudnorm.
set -euo pipefail
VERSION=8.1.3-1
SHA256=87bedcefc860cd234cdeddbebdcd3ce45aeedc81d0fbb258ed839ff778be3140
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p /out
# jellyfin-ffmpeg is installed on amd64 only, so the fast build could never stand in for it elsewhere.
[ "$(dpkg --print-architecture)" = amd64 ] || exit 0
cd /tmp
curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --retry-connrefused -o jellyfin-ffmpeg.tar.gz "https://github.com/jellyfin/jellyfin-ffmpeg/archive/refs/tags/v${VERSION}.tar.gz"
echo "${SHA256}  jellyfin-ffmpeg.tar.gz" | sha256sum -c -
tar -xzf jellyfin-ffmpeg.tar.gz
cd "jellyfin-ffmpeg-${VERSION}"
while read -r p; do
  [ -n "$p" ] && [ "${p#\#}" = "$p" ] && patch -p1 --quiet < "debian/patches/$p"
done < debian/patches/series
patch -p1 < "$here/0001-avfilter-ebur128-cache-100ms-block-energies.patch"
./configure --extra-version=Jellyfin --enable-gpl --enable-version3 --disable-unstable --disable-autodetect --enable-zlib --enable-bzlib --disable-network --disable-shared \
  --disable-doc --disable-ffprobe --disable-ffplay --disable-debug
make -j"$(nproc)"
cp ffmpeg /out/ffmpeg && strip /out/ffmpeg
echo "$VERSION" > /out/jellyfin-release
/out/ffmpeg -hide_banner -version | head -1
