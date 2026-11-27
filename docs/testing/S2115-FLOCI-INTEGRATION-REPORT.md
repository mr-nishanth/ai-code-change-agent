# S2115 integration report: Floci local AWS emulator

Status: **filled from the S2115 Task 8 clean run on 2026-11-27**. Every value below was observed in that run.

Related: [AWS-DEPLOYMENT-READINESS.md](../aws/AWS-DEPLOYMENT-READINESS.md) ·
[FLOCI-SERVICE-CAPABILITY-MATRIX.md](../aws/FLOCI-SERVICE-CAPABILITY-MATRIX.md) ·
[ADR-014](../decisions/ADR-014-floci-local-aws-emulator.md).

## 1. Environment

| Item | Value |
|---|---|
| Date | 2026-11-27 (UTC) |
| Commit | `944a55c` (tree under test; this report is committed on top of it) |
| Host | macOS 27.2.1, arm64 |
| Docker | Engine 29.9.2 linux/arm64, containerd image store (`io.containerd.snapshotter.v1`) |
| Floci image | `floci/floci:2.5.0@sha256:a0fbf944d7f338cc0856f1ef83831ea749ff2be71956eb7a7fce08fc21458e19` |
| aws CLI / boto3 | aws-cli/2.31.37; boto3 1.44.11, botocore 1.44.11 |
| Terraform | v1.17.2 darwin_arm64 |
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
docker compose -f infra/docker-compose.yml down   # then: uv run pytest -m floci -rs --no-cov (all skip)
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
| `tests/integration/floci/test_floci_smoke.py::test_sts_accepts_dummy_credentials` | STS | PASSED | 0.03 s |  |
| `tests/integration/floci/test_floci_smoke.py::test_s3_object_round_trip` | S3 | PASSED | 0.05 s |  |
| `tests/integration/floci/test_floci_core.py::test_sts_identity_and_iam_role_with_inline_policy` | STS, IAM | PASSED | 0.11 s | IAM API surface only; enforcement off |
| `tests/integration/floci/test_floci_core.py::test_s3_object_lifecycle_and_presigned_get` | S3 | PASSED | 0.06 s |  |
| `tests/integration/floci/test_floci_core.py::test_sqs_dead_letter_redrive_and_move_back` | SQS | PASSED | 4.11 s |  |
| `tests/integration/floci/test_floci_core.py::test_secrets_manager_versions` | Secrets Manager | PASSED | 0.09 s |  |
| `tests/integration/floci/test_floci_core.py::test_cloudwatch_logs_put_get_and_filter` | CloudWatch Logs | PASSED | 0.05 s |  |
| `tests/integration/floci/test_floci_core.py::test_cloudwatch_metrics_put_query_and_list` | CloudWatch Metrics | PASSED | 0.04 s |  |
| `tests/integration/floci/test_floci_containers.py::test_ecr_push_and_pull_by_digest` | ECR | PASSED | 2.64 s (+0.16 s setup) | path-style `localhost:4566/...`; isolated `DOCKER_CONFIG` |
| `tests/integration/floci/test_floci_containers.py::test_ecs_task_exit_code_and_stop` | ECS | PASSED | 11.32 s | `launchType=EC2`; exit code 7 and `stoppedReason` observed; task containers gone afterwards |
| `tests/integration/floci/test_floci_containers.py::test_rds_postgres_create_connect_query_delete` | RDS | PASSED | 1.69 s | port in 7001-7005; data volume pruned on delete |
| `tests/integration/floci/test_floci_containers.py::test_lambda_invoke_and_api_gateway_http_and_rest` | Lambda, API Gateway | PASSED | 0.36 s (+0.08 s setup, 1.10 s teardown) | v2 via `Host` header; v1 via `/restapis/{id}/dev/_user_request_/hello` |

`uv run pytest -m floci -rs --no-cov`: **12 passed**, 1552 deselected in 22.23 s.

Full suite with Floci up (`uv run pytest`): **1564 passed** in 113.24 s, total coverage **91.87 %**
(required 90 %). Floci stopped: all 12 `floci` tests **SKIPPED** with
`Floci not reachable at http://127.0.0.1:4566: ConnectError`, 1552 deselected.

Bootstrap: first run created 2 queues (redrive `maxReceiveCount` 3), 2 buckets, 1 ECR repository,
1 secret and 2 log groups (retention 1 day); the second run reported every resource as
`exists` / `up to date` (idempotent). `ruff check`, `ruff format --check` (341 files) and `mypy`
(303 source files) clean; `terraform -chdir=infra/terraform/local validate` succeeded.

## 4. Deviations from the plan

1. **No Floci behaviour differed from the documentation** for anything the tests assert; no matrix row
   was downgraded.
2. **ECR sidecar outlives Compose.** `floci-aws-ecr-registry` and `floci-aws-ecr-registry-data` are
   created by Floci through the socket, not by Compose. `docker compose -f infra/docker-compose.yml
   down -v` removed `acca-floci` and `acca-floci_acca_floci_data` but printed
   `Network acca-floci Resource is still in use`: the sidecar keeps the network. The next `up` reused
   both without error. A fully clean slate needs
   `docker rm -f floci-aws-ecr-registry && docker volume rm floci-aws-ecr-registry-data` (then the
   network can be removed). Not done here, to keep the plan's expectation (sidecar may remain).
3. **Shell environment matters for unit tests.** Running the full suite with dummy AWS credentials
   *and* `AWS_CONFIG_FILE=/dev/null` / `AWS_SHARED_CREDENTIALS_FILE=/dev/null` exported failed three
   pre-existing `tests/unit/test_llm_provider.py` Bedrock tests (`ChatBedrockConverse`: "both
   aws_access_key_id and aws_secret_access_key must be specified"); each variable set alone did not.
   The Floci tests pass explicit credentials to boto3, so run `uv run pytest` with the AWS variables
   unset (as above), and only the bootstrap scripts (which export them themselves) need them.
4. Floci was stopped with `docker compose -f infra/docker-compose.yml down` (no `-v`): the
   `acca-floci_acca_floci_data` volume is kept for the next run.

## 5. Security and cleanup evidence

| Check | Expected | Observed |
|---|---|---|
| `docker port acca-floci` | only `127.0.0.1` bindings | `4566/tcp -> 127.0.0.1:4566`, `7001..7005/tcp -> 127.0.0.1:7001..7005`; nothing else |
| `docker inspect acca-api` mounts | no `docker.sock` | `[]` (no mounts at all) |
| Floci child containers after the run | only `floci-aws-ecr-registry` (ECR sidecar) | `docker ps -a --filter label=floci=true`: only `floci-aws-ecr-registry` |
| Floci volumes (`label=floci=true`) | only `floci-aws-ecr-registry-data` | only `floci-aws-ecr-registry-data` (RDS volume pruned by the test) |
| Application stack (`acca-api`, `acca-postgres`, `acca_pgdata`) | untouched | `acca-api` started 2026-11-27T08:08:43Z and `acca-postgres` 2026-11-27T04:39:23Z, both before and after the run; `ai-code-change-agent_acca_pgdata` present |

## Controller follow-up (2026-11-27)
- The Bedrock unit tests that failed only with dummy AWS variables exported now ignore the shell: `tests/conftest.py` clears ambient `AWS_*` variables before every test. Full suite with the Floci dummy variables exported: 1,564 passed, 0 skipped, coverage 91.87%.
- Floci was restarted after Task 8 and is running and healthy (acceptance criterion 1).
