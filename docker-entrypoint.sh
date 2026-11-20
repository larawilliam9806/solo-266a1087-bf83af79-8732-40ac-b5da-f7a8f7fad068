#!/bin/sh
set -eu

SEED_DIR="${SEED_DIR:-/seed}"

# Seed the writable data volume with the bundled demo catalog on first run.
if [ -n "${CATALOG_FILE:-}" ] && [ ! -f "$CATALOG_FILE" ] && [ -f "$SEED_DIR/catalog.json" ]; then
  mkdir -p "$(dirname "$CATALOG_FILE")"
  cp "$SEED_DIR/catalog.json" "$CATALOG_FILE"
fi

exec "$@"
