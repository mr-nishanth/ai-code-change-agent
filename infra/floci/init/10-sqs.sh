#!/bin/sh
# SQS: job queue and its dead-letter queue (docs/aws/DYNAMODB-DATA-MODEL.md section 1).
# Job queue: VisibilityTimeout 300 s (the job lease), ReceiveMessageWaitTimeSeconds 20 (long
# polling), redrive to the DLQ after maxReceiveCount 20. Existing queues are updated in place.
# Name: ACCA_SQS_JOB_QUEUE_NAME (default acca-local-jobs); the DLQ is "<name>-dlq".
set -eu
ACCA_FLOCI_INIT_DIR=${ACCA_FLOCI_INIT_DIR:-$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)}
# shellcheck source=infra/floci/init/_env.sh
. "$ACCA_FLOCI_INIT_DIR/_env.sh"

jobs_queue=${ACCA_SQS_JOB_QUEUE_NAME:-acca-local-jobs}
dlq_queue="$jobs_queue-dlq"
visibility_timeout=300
receive_wait=20
max_receive_count=20

ensure_queue() {
  if url=$(awsl sqs get-queue-url --queue-name "$1" --query QueueUrl --output text 2>/dev/null); then
    log "sqs queue $1 exists" >&2
  else
    url=$(awsl sqs create-queue --queue-name "$1" --query QueueUrl --output text)
    log "sqs queue $1 created" >&2
  fi
  echo "$url"
}

queue_attribute() {
  awsl sqs get-queue-attributes --queue-url "$1" --attribute-names "$2" \
    --query "Attributes.$2" --output text
}

dlq_url=$(ensure_queue "$dlq_queue")
dlq_arn=$(queue_attribute "$dlq_url" QueueArn)
jobs_url=$(ensure_queue "$jobs_queue")

current_visibility=$(queue_attribute "$jobs_url" VisibilityTimeout)
current_wait=$(queue_attribute "$jobs_url" ReceiveMessageWaitTimeSeconds)
current_redrive=$(queue_attribute "$jobs_url" RedrivePolicy)
redrive_ok=no
case "$current_redrive" in
  *"\"$dlq_arn\""*"\"maxReceiveCount\":\"$max_receive_count\""* | \
    *"\"$dlq_arn\""*"\"maxReceiveCount\":$max_receive_count}"* | \
    *"\"maxReceiveCount\":\"$max_receive_count\""*"\"$dlq_arn\""* | \
    *"\"maxReceiveCount\":$max_receive_count,"*"\"$dlq_arn\""*)
    redrive_ok=yes
    ;;
esac

if [ "$current_visibility" = "$visibility_timeout" ] && [ "$current_wait" = "$receive_wait" ] &&
  [ "$redrive_ok" = yes ]; then
  log "sqs queue $jobs_queue attributes up to date"
else
  redrive=$(printf '{\\"deadLetterTargetArn\\":\\"%s\\",\\"maxReceiveCount\\":\\"%s\\"}' \
    "$dlq_arn" "$max_receive_count")
  attrs=$(printf '{"VisibilityTimeout":"%s","ReceiveMessageWaitTimeSeconds":"%s","RedrivePolicy":"%s"}' \
    "$visibility_timeout" "$receive_wait" "$redrive")
  awsl sqs set-queue-attributes --queue-url "$jobs_url" --attributes "$attrs"
  log "sqs queue $jobs_queue set: VisibilityTimeout $visibility_timeout, ReceiveMessageWaitTimeSeconds $receive_wait, redrive to $dlq_arn after $max_receive_count receives"
fi
