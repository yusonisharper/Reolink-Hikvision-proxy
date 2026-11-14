#!/bin/sh
# Builds libhevcmerge-<cpu>.so next to this script (loaded by bcproxy/hevcmerge.py).
#   build.sh            for this machine, with cc
#   build.sh aarch64    for the Cloud Key (UCK-G2-Plus), with aarch64-linux-gnu-gcc
#                       (Debian/Ubuntu: apt install gcc-aarch64-linux-gnu)
# CC=... picks another compiler.
set -eu
cd "$(dirname "$0")"
ARCH=${1:-$(uname -m)}
if [ -z "${CC:-}" ]; then
    if [ "$ARCH" = "$(uname -m)" ]; then CC=cc; else CC=$ARCH-linux-gnu-gcc; fi
fi
FLAGS="-O2 -pthread -fPIC -shared -fvisibility=hidden -Wall -Wextra"
case $ARCH in
    aarch64) FLAGS="$FLAGS -mcpu=cortex-a53" ;;
esac
# shellcheck disable=SC2086
$CC $FLAGS -o "libhevcmerge-$ARCH.so" hevcmerge.c
echo "built $(pwd)/libhevcmerge-$ARCH.so"
