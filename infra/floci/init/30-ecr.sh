#!/bin/sh
# ECR: smoke-test repository.
set -eu
ACCA_FLOCI_INIT_DIR=${ACCA_FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$ACCA_FLOCI_INIT_DIR/_env.sh"

repo=acca/local-smoke
if awsl ecr describe-repositories --repository-names "$repo" >/dev/null 2>&1; then
  log "ecr repository $repo exists"
else
  awsl ecr create-repository --repository-name "$repo" >/dev/null
  log "ecr repository $repo created"
fi
