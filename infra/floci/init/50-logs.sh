#!/bin/sh
# CloudWatch Logs: worker and API log groups with 1-day retention.
set -eu
FLOCI_INIT_DIR=${FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$FLOCI_INIT_DIR/_env.sh"

for group in /acca/local/worker /acca/local/api; do
  found=$(awsl logs describe-log-groups --log-group-name-prefix "$group" \
    --query "logGroups[?logGroupName=='$group'].logGroupName" --output text)
  if [ "$found" = "$group" ]; then
    log "log group $group exists"
  else
    awsl logs create-log-group --log-group-name "$group"
    log "log group $group created"
  fi
  retention=$(awsl logs describe-log-groups --log-group-name-prefix "$group" \
    --query "logGroups[?logGroupName=='$group'].retentionInDays" --output text)
  if [ "$retention" = "1" ]; then
    log "log group $group retention 1 day up to date"
  else
    awsl logs put-retention-policy --log-group-name "$group" --retention-in-days 1
    log "log group $group retention set to 1 day"
  fi
done
