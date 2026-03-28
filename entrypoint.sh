#!/bin/sh
# Fix ownership of /config when mounted as a volume (host may own it as root)
if [ "$(id -u)" = "0" ]; then
    chown -R appuser:appuser /config
    exec su-exec appuser "$@"
else
    exec "$@"
fi
