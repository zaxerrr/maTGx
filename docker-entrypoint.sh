#!/bin/sh
# Bind-mounted ./logs and ./state are created by Docker as root. Fix their
# ownership, then drop privileges and run the bridge as the unprivileged
# "app" user.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p /app/logs /app/state
    chown -R app:app /app/logs /app/state
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
