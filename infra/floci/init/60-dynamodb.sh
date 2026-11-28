#!/bin/sh
# DynamoDB: the application table and the LangGraph checkpoint table
# (docs/aws/DYNAMODB-DATA-MODEL.md sections 1, 6 and 7). Key schema, GSIs and billing mode come
# from infra/floci/schema/*.json (CreateTable input without TableName); TTL is enabled here because
# CreateTable cannot set it. Creates a missing table. An existing table whose index layout (GSI
# names, keys, projections) differs from the schema file is refused (exit 5): DynamoDB cannot change
# a GSI's projection in place, so the table has to be recreated. Floci holds local dev data only,
# so that is one explicit command (it deletes the table's items):
#   ACCA_DYNAMODB_RECREATE_ON_DRIFT=1 sh infra/floci/init/60-dynamodb.sh
# The layout is compared with a short-lived probe table created from the same schema file.
# Names: ACCA_DYNAMODB_TABLE_NAME (default acca-local-app) and
# ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME (default acca-local-checkpoints).
set -eu
ACCA_FLOCI_INIT_DIR=${ACCA_FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$ACCA_FLOCI_INIT_DIR/_env.sh"

schema_dir=$(CDPATH='' cd -- "$ACCA_FLOCI_INIT_DIR/../schema" && pwd)

create_table() {
  awsl dynamodb create-table --table-name "$1" \
    --cli-input-json "file://$schema_dir/$2" >/dev/null
  awsl dynamodb wait table-exists --table-name "$1"
}

# The table's GSI layout (sorted by name: keys and projection) as one comparable line.
gsi_layout() {
  awsl dynamodb describe-table --table-name "$1" --output json \
    --query 'sort_by(Table.GlobalSecondaryIndexes || `[]`, &IndexName)[].[IndexName, KeySchema, Projection]' |
    tr -d ' \n\t'
}

# The GSI layout the schema file produces, read back from a probe table.
wanted_layout() {
  probe="$1-schema-probe"
  create_table "$probe" "$2"
  layout=$(gsi_layout "$probe")
  awsl dynamodb delete-table --table-name "$probe" >/dev/null
  echo "$layout"
}

ensure_table() {
  name=$1
  schema=$2
  ttl_attribute=$3
  if awsl dynamodb describe-table --table-name "$name" >/dev/null 2>&1; then
    if [ "$(gsi_layout "$name")" = "$(wanted_layout "$name" "$schema")" ]; then
      log "dynamodb table $name exists"
    elif [ "${ACCA_DYNAMODB_RECREATE_ON_DRIFT:-}" = 1 ]; then
      awsl dynamodb delete-table --table-name "$name" >/dev/null
      awsl dynamodb wait table-not-exists --table-name "$name"
      create_table "$name" "$schema"
      log "dynamodb table $name recreated from $schema (index layout changed)"
    else
      echo "floci-init: dynamodb table $name has another index layout than $schema;" \
        "recreate it (local data is lost): ACCA_DYNAMODB_RECREATE_ON_DRIFT=1 sh $0" >&2
      return 5
    fi
  else
    create_table "$name" "$schema"
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
