#!/bin/sh
# Secrets Manager: one test-only dummy secret. Never seed real secrets here and
# never read .env; this value is not a credential.
set -eu
ACCA_FLOCI_INIT_DIR=${ACCA_FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$ACCA_FLOCI_INIT_DIR/_env.sh"

name=acca/local/test-only-dummy
if awsl secretsmanager describe-secret --secret-id "$name" >/dev/null 2>&1; then
  log "secret $name exists"
else
  awsl secretsmanager create-secret --name "$name" \
    --description "Local Floci test-only dummy, not a credential" \
    --secret-string '{"note":"test-only, not a credential"}' >/dev/null
  log "secret $name created"
fi
