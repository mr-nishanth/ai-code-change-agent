# S2115 integration report: Floci local AWS emulator

Status: **skeleton**. Task 8 of the S2115 plan fills it from a clean run. Do not record a result here
that was not observed.

Related: [AWS-DEPLOYMENT-READINESS.md](AWS-DEPLOYMENT-READINESS.md) ·
[FLOCI-SERVICE-CAPABILITY-MATRIX.md](FLOCI-SERVICE-CAPABILITY-MATRIX.md) ·
[ADR-014](../decisions/ADR-014-floci-local-aws-emulator.md).

## 1. Environment

| Item | Value |
|---|---|
| Date | _TBD_ |
| Commit | _TBD_ (`git rev-parse --short HEAD`) |
| Host | _TBD_ (OS, CPU architecture) |
| Docker | _TBD_ (`docker version --format '{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}'`; image store) |
| Floci image | `floci/floci:2.5.0@sha256:a0fbf944d7f338cc0856f1ef83831ea749ff2be71956eb7a7fce08fc21458e19` |
| aws CLI / boto3 | _TBD_ |
| Terraform | _TBD_ (`terraform version`) |
| Credentials | `test`/`test`, `us-east-1`, endpoint `http://127.0.0.1:4566` only |

## 2. Commands

Run with `AWS_PROFILE` and real AWS variables unset.

```bash
docker compose -f infra/docker-compose.yml down -v
docker compose -f infra/docker-compose.yml up -d --wait
curl -fsS http://127.0.0.1:4566/_floci/health
sh infra/floci/init/bootstrap.sh            # twice: must be idempotent
sh infra/floci/init/bootstrap.sh
uv run pytest -m floci -rs --no-cov
uv run pytest                                # full suite, coverage >= 90 %
uv run ruff check . && uv run ruff format --check . && uv run mypy
terraform -chdir=infra/terraform/local validate
docker port acca-floci
docker inspect acca-api --format '{{json .Mounts}}'
docker ps -a --filter label=floci=true
docker volume ls --filter label=floci=true
```

## 3. Per-test results

| Test | Service | Result | Duration | Notes |
|---|---|---|---|---|
| `tests/integration/floci/test_floci_smoke.py::test_sts_accepts_dummy_credentials` | STS | _TBD_ | | |
| `tests/integration/floci/test_floci_smoke.py::test_s3_object_round_trip` | S3 | _TBD_ | | |
| `tests/integration/floci/test_floci_core.py::test_sts_identity_and_iam_role_with_inline_policy` | STS, IAM | _TBD_ | | |
| `tests/integration/floci/test_floci_core.py::test_s3_object_lifecycle_and_presigned_get` | S3 | _TBD_ | | |
| `tests/integration/floci/test_floci_core.py::test_sqs_dead_letter_redrive_and_move_back` | SQS | _TBD_ | | |
| `tests/integration/floci/test_floci_core.py::test_secrets_manager_versions` | Secrets Manager | _TBD_ | | |
| `tests/integration/floci/test_floci_core.py::test_cloudwatch_logs_put_get_and_filter` | CloudWatch Logs | _TBD_ | | |
| `tests/integration/floci/test_floci_core.py::test_cloudwatch_metrics_put_query_and_list` | CloudWatch Metrics | _TBD_ | | |
| `tests/integration/floci/test_floci_containers.py::test_ecr_push_and_pull_by_digest` | ECR | _TBD_ | | |
| `tests/integration/floci/test_floci_containers.py::test_ecs_task_exit_code_and_stop` | ECS | _TBD_ | | |
| `tests/integration/floci/test_floci_containers.py::test_rds_postgres_create_connect_query_delete` | RDS | _TBD_ | | |
| `tests/integration/floci/test_floci_containers.py::test_lambda_invoke_and_api_gateway_http_and_rest` | Lambda, API Gateway | _TBD_ | | |

Full suite: _TBD_ (passed / skipped / coverage). Floci stopped: all `floci` tests _TBD_ (expected SKIPPED with a reason).

## 4. Deviations from the plan

_TBD_ (anything that behaved differently from the Floci documentation or the plan, and what was done).

## 5. Security and cleanup evidence

| Check | Expected | Observed |
|---|---|---|
| `docker port acca-floci` | only `127.0.0.1` bindings | _TBD_ |
| `docker inspect acca-api` mounts | no `docker.sock` | _TBD_ |
| Floci child containers after the run | only `floci-aws-ecr-registry` (ECR sidecar) | _TBD_ |
| Floci volumes (`label=floci=true`) | only `floci-aws-ecr-registry-data` | _TBD_ |
| Application stack (`acca-api`, `acca-postgres`, `acca_pgdata`) | untouched | _TBD_ |
