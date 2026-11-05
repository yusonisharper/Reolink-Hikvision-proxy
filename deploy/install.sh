#!/bin/sh
# Runs on the Cloud Key. Safe to repeat, also after a firmware update: everything that this needs
# is under /data, so without the service file it needs no network.
#
# Firmware updates wipe /usr, /lib and /var and keep /data and /etc/systemd/system (see
# overlay_cleanup in /usr/share/initramfs-tools/scripts/ubnt-init/apq8053/boot-emmc). So nothing
# goes through apt or into /usr, and Python is our own copy, not the firmware's.
set -eu

ROOT=/data/bcproxy
UNIT=/etc/systemd/system/bcproxy.service

PY_NAME=cpython-3.13.16+20261013-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/20261013/cpython-3.13.16%2B20261013-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
PY_SHA256=69a30ed26198178732c37b8de0b6b5191479fbacfe526954da776befe38f18e7
FF_URL=https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-arm64-static.tar.xz
FF_SHA256=f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1

fetch() {   # url sha256 output
    curl -fsSL --retry 3 -o "$3" "$1"
    echo "$2  $3" | sha256sum -c - >/dev/null || { echo "checksum mismatch: $1" >&2; rm -f "$3"; exit 1; }
}

mkdir -p "$ROOT/bin" "$ROOT/logs" "$ROOT/state"
chmod 700 "$ROOT"
TMP=$(mktemp -d "$ROOT/.install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

if [ ! -x "$ROOT/python/bin/python3" ]; then
    echo "installing Python: $PY_NAME"
    fetch "$PY_URL" "$PY_SHA256" "$TMP/python.tgz"
    tar -xzf "$TMP/python.tgz" -C "$ROOT"
fi
if ! "$ROOT/python/bin/python3" -c "import yaml, cryptography" 2>/dev/null; then
    echo "installing Python packages"
    "$ROOT/python/bin/python3" -m pip install --quiet --no-warn-script-location --root-user-action=ignore \
        -r "$ROOT/app/requirements.txt"
fi
if [ ! -x "$ROOT/bin/ffmpeg" ] || [ ! -x "$ROOT/bin/ffprobe" ]; then
    echo "installing ffmpeg"
    fetch "$FF_URL" "$FF_SHA256" "$TMP/ffmpeg.txz"
    tar -xJf "$TMP/ffmpeg.txz" -C "$TMP" --strip-components=1 --wildcards '*/ffmpeg' '*/ffprobe'
    mv "$TMP/ffmpeg" "$TMP/ffprobe" "$ROOT/bin/"
fi

# a real file, not a link into /data: systemd scans units before /data is mounted
install -m 644 "$ROOT/app/deploy/bcproxy.service" "$UNIT"
systemctl daemon-reload
systemctl enable bcproxy.service

if [ -f "$ROOT/config.yaml" ]; then
    chmod 600 "$ROOT/config.yaml"
    systemctl restart bcproxy.service
    echo "bcproxy: $(systemctl is-active bcproxy.service)"
else
    echo "no $ROOT/config.yaml yet: not starting" >&2
fi
