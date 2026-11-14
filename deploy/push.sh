#!/bin/sh
# Runs on the PC, from anywhere: copies the code to the Cloud Key and installs or restarts it.
#   deploy/push.sh [--with-config] [ssh target, default UCK-G2-Plus]
# --with-config also copies config.yaml, state/ and captured/, replacing those on the device. Use
# it for the first install: afterwards the device's state/ is the current one.
# SSH_OPTS can add ssh options, such as -i <key>.
set -eu

WITH_CONFIG=no
if [ "${1:-}" = "--with-config" ]; then
    WITH_CONFIG=yes
    shift
fi
HOST=${1:-UCK-G2-Plus}
cd "$(dirname "$0")/.."
ROOT=/data/bcproxy

# H.265 tile merging (bcproxy/hevcmerge.py) needs the library built for the Cloud Key's CPU
LIB=bcproxy/native/libhevcmerge-aarch64.so
if [ ! -f "$LIB" ] || [ bcproxy/native/hevcmerge.c -nt "$LIB" ]; then
    echo "warning: $LIB is missing or older than hevcmerge.c; run bcproxy/native/build.sh aarch64" >&2
fi

# Swapped in as a whole: a running proxy keeps its loaded code, and a broken copy changes nothing
# shellcheck disable=SC2086
tar -cf - --exclude=__pycache__ --exclude='*.pyc' bcproxy tools deploy requirements.txt | ssh ${SSH_OPTS:-} "$HOST" "
    set -e
    umask 077
    mkdir -p $ROOT/app.new
    tar -xf - -C $ROOT/app.new
    rm -rf $ROOT/app.old
    if [ -d $ROOT/app ]; then mv $ROOT/app $ROOT/app.old; fi
    mv $ROOT/app.new $ROOT/app
    rm -rf $ROOT/app.old"

if [ "$WITH_CONFIG" = yes ]; then
    dirs=
    for d in state captured; do
        if [ -d "$d" ]; then dirs="$dirs $d"; fi
    done
    # shellcheck disable=SC2086
    tar -cf - config.yaml $dirs | ssh ${SSH_OPTS:-} "$HOST" "umask 077; tar -xf - -C $ROOT; chmod 600 $ROOT/config.yaml"
fi

# shellcheck disable=SC2086
ssh ${SSH_OPTS:-} "$HOST" "sh $ROOT/app/deploy/install.sh"
