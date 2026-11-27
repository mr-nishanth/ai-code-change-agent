#!/bin/sh
# SQS: job queue with a dead-letter queue (maxReceiveCount 3).
set -eu
FLOCI_INIT_DIR=${FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$FLOCI_INIT_DIR/_env.sh"

ensure_queue() {
  if url=$(awsl sqs get-queue-url --queue-name "$1" --query QueueUrl --output text 2>/dev/null); then
    log "sqs queue $1 exists" >&2
  else
    url=$(awsl sqs create-queue --queue-name "$1" --query QueueUrl --output text)
    log "sqs queue $1 created" >&2
  fi
  echo "$url"
}

dlq_url=$(ensure_queue acca-local-jobs-dlq)
dlq_arn=$(awsl sqs get-queue-attributes --queue-url "$dlq_url" \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)
jobs_url=$(ensure_queue acca-local-jobs)

redrive="{\"deadLetterTargetArn\":\"$dlq_arn\",\"maxReceiveCount\":\"3\"}"
current=$(awsl sqs get-queue-attributes --queue-url "$jobs_url" \
  --attribute-names RedrivePolicy --query Attributes.RedrivePolicy --output text)
case "$current" in
  *"$dlq_arn"*'"maxReceiveCount":"3"'* | *"$dlq_arn"*'"maxReceiveCount":3'*)
    log "sqs redrive policy on acca-local-jobs up to date"
    ;;
  *)
    attrs=$(printf '{"RedrivePolicy":"%s"}' "$(printf '%s' "$redrive" | sed 's/"/\\"/g')")
    awsl sqs set-queue-attributes --queue-url "$jobs_url" --attributes "$attrs"
    log "sqs redrive policy on acca-local-jobs set (dlq $dlq_arn, maxReceiveCount 3)"
    ;;
esac
