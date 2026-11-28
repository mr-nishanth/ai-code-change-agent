#!/bin/sh
# Create the local-only AWS resources in Floci. Safe to run repeatedly.
# Usage: sh infra/floci/init/bootstrap.sh
set -eu

ACCA_FLOCI_INIT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
export ACCA_FLOCI_INIT_DIR

# shellcheck source=infra/floci/init/_env.sh
. "$ACCA_FLOCI_INIT_DIR/_env.sh"

for script in "$ACCA_FLOCI_INIT_DIR"/[0-9][0-9]-*.sh; do
  [ -f "$script" ] || continue
  log "running $(basename -- "$script")"
  sh "$script"
done
log "bootstrap complete"
