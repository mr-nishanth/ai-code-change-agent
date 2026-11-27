# Mirrors infra/floci/init (Task 2) with a "tf-" prefix so it never collides
# with the bootstrap scripts' resources. Validate-only: not applied.

locals {
  prefix = "tf-acca"
}

# SQS: job queue with a dead-letter queue (maxReceiveCount 3).
resource "aws_sqs_queue" "jobs_dlq" {
  name = "${local.prefix}-local-jobs-dlq"
}

resource "aws_sqs_queue" "jobs" {
  name = "${local.prefix}-local-jobs"
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jobs_dlq.arn
    maxReceiveCount     = 3
  })
}

# S3: artifact and scratch buckets.
resource "aws_s3_bucket" "local" {
  for_each      = toset(["artifacts", "scratch"])
  bucket        = "${local.prefix}-local-${each.key}"
  force_destroy = true
}

# ECR: smoke-test repository.
resource "aws_ecr_repository" "smoke" {
  name         = "${local.prefix}/local-smoke"
  force_delete = true
}

# Secrets Manager: one test-only dummy secret. Never a real credential.
resource "aws_secretsmanager_secret" "dummy" {
  name                    = "${local.prefix}/local/test-only-dummy"
  description             = "Local Floci test-only dummy, not a credential"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "dummy" {
  secret_id     = aws_secretsmanager_secret.dummy.id
  secret_string = jsonencode({ note = "test-only, not a credential" })
}

# CloudWatch Logs: worker and API log groups with 1-day retention.
resource "aws_cloudwatch_log_group" "local" {
  for_each          = toset(["worker", "api"])
  name              = "/${local.prefix}/local/${each.key}"
  retention_in_days = 1
}
