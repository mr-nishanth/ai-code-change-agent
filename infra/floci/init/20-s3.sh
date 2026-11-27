#!/bin/sh
# S3: artifact and scratch buckets.
set -eu
FLOCI_INIT_DIR=${FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$FLOCI_INIT_DIR/_env.sh"

for bucket in acca-local-artifacts acca-local-scratch; do
  if awsl s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    log "s3 bucket $bucket exists"
  else
    awsl s3api create-bucket --bucket "$bucket" >/dev/null
    log "s3 bucket $bucket created"
  fi
done
