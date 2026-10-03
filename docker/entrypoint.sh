#!/bin/sh
# Runs as root only long enough to make /data writable, then drops to an
# unprivileged user. (The Unreal server refuses to run as root.)
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    groupmod -o -g "$PGID" vsm >/dev/null
    usermod -o -u "$PUID" vsm >/dev/null 2>&1
    mkdir -p "$VSM_DATA_DIR"
    if [ "$(stat -c %u:%g "$VSM_DATA_DIR")" != "$PUID:$PGID" ]; then
        chown -R vsm:vsm "$VSM_DATA_DIR"
    fi
    chown -R vsm:vsm /home/vsm
    exec gosu vsm "$@"
fi

exec "$@"
