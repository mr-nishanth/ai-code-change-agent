#!/bin/sh
# DynamoDB: the application table and the LangGraph checkpoint table
# (docs/aws/DYNAMODB-DATA-MODEL.md sections 1, 6 and 7). Key schema, GSIs and billing mode come
# from infra/floci/schema/*.json (CreateTable input without TableName); TTL is enabled here because
# CreateTable cannot set it. Creates a missing table, never alters or drops an existing one.
# Names: ACCA_DYNAMODB_TABLE_NAME (default acca-local-app) and
# ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME (default acca-local-checkpoints).
set -eu
ACCA_FLOCI_INIT_DIR=${ACCA_FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$ACCA_FLOCI_INIT_DIR/_env.sh"

schema_dir=$(CDPATH='' cd -- "$ACCA_FLOCI_INIT_DIR/../schema" && pwd)

ensure_table() {
  name=$1
  schema=$2
  ttl_attribute=$3
  if awsl dynamodb describe-table --table-name "$name" >/dev/null 2>&1; then
    log "dynamodb table $name exists"
  else
    awsl dynamodb create-table --table-name "$name" \
      --cli-input-json "file://$schema_dir/$schema" >/dev/null
    awsl dynamodb wait table-exists --table-name "$name"
    log "dynamodb table $name created from $schema"
  fi
  ttl_status=$(awsl dynamodb describe-time-to-live --table-name "$name" \
    --query TimeToLiveDescription.TimeToLiveStatus --output text)
  ttl_name=$(awsl dynamodb describe-time-to-live --table-name "$name" \
    --query TimeToLiveDescription.AttributeName --output text)
  case "$ttl_status" in
    ENABLED | ENABLING)
      if [ "$ttl_name" = "$ttl_attribute" ]; then
        log "dynamodb table $name ttl on $ttl_attribute up to date"
        return 0
      fi
      echo "floci-init: dynamodb table $name has ttl on '$ttl_name', expected '$ttl_attribute'" >&2
      return 4
      ;;
  esac
  awsl dynamodb update-time-to-live --table-name "$name" \
    --time-to-live-specification "Enabled=true,AttributeName=$ttl_attribute" >/dev/null
  log "dynamodb table $name ttl enabled on $ttl_attribute"
}

ensure_table "${ACCA_DYNAMODB_TABLE_NAME:-acca-local-app}" acca-app.json expires_at
ensure_table "${ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME:-acca-local-checkpoints}" acca-checkpoints.json ttl
