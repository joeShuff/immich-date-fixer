#!/bin/sh
# Bind-mounted folders are often created by Docker as root, which the unprivileged
# app user cannot write to. Fix ownership of the data folder, then drop privileges.
set -e
if [ "$(id -u)" = "0" ]; then
  mkdir -p "${DATA_DIR:-/data}"
  chown -R app "${DATA_DIR:-/data}"
  exec su-exec app "$@"
fi
exec "$@"