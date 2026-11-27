# AWS deployment readiness (S2115)

Status: 2026-11-27. Branch `feature/s2115-floci-local-aws`.

S2115 adds a **local, test-only** AWS emulator ([Floci](https://github.com/floci-io/floci) 2.5.0) and
tests that exercise the AWS service shapes a future deployment could use. **The application itself
does not change**: no `src` code, `Settings` field, Alembic migration or `compose.yaml` line was
added, and the API, worker, sandboxes and providers run exactly as before.

Related: [ADR-014](../decisions/ADR-014-floci-local-aws-emulator.md) ·
[architecture audit](S2115-ARCHITECTURE-AUDIT.md) ·
[service capability matrix](FLOCI-SERVICE-CAPABILITY-MATRIX.md) ·
[integration report](../testing/S2115-FLOCI-INTEGRATION-REPORT.md).

## 1. What is proven locally

Everything below runs against Floci on `http://127.0.0.1:4566` with the dummy credentials
`test`/`test` (region `us-east-1`, account `000000000000`). Nothing reaches a real AWS account.

| Area | Proven by | What it shows |
|---|---|---|
| Emulator runtime | [`infra/docker-compose.yml`](../../infra/docker-compose.yml) | Pinned image digest, loopback-only ports, own project `acca-floci`, own network and volume, persistent storage, health check |
| Idempotent bootstrap | [`infra/floci/init/bootstrap.sh`](../../infra/floci/init/bootstrap.sh) | SQS + DLQ, S3 buckets, ECR repository, dummy secret, log groups; safe to rerun; refuses a non-loopback endpoint |
| STS / IAM | `tests/integration/floci/test_floci_core.py::test_sts_identity_and_iam_role_with_inline_policy` | Caller identity; role and inline policy CRUD (IAM policies are **not enforced** by default) |
| S3 | `...test_floci_core.py::test_s3_object_lifecycle_and_presigned_get` | Put, head (ETag), get, list, pre-signed GET, delete |
| SQS + DLQ | `...test_floci_core.py::test_sqs_dead_letter_redrive_and_move_back` | `maxReceiveCount` redrive to the DLQ and `StartMessageMoveTask` back |
| Secrets Manager | `...test_floci_core.py::test_secrets_manager_versions` | Create, get, new version with `AWSCURRENT`, describe, force delete |
| CloudWatch Logs | `...test_floci_core.py::test_cloudwatch_logs_put_get_and_filter` | Groups, streams, put, get, filter |
| CloudWatch Metrics | `...test_floci_core.py::test_cloudwatch_metrics_put_query_and_list` | Put, statistics, `GetMetricData`, list |
| ECR | `tests/integration/floci/test_floci_containers.py::test_ecr_push_and_pull_by_digest` | `docker login`/push/pull through `:4566`, digest consistency, pull by `@sha256:` |
| ECS | `...test_floci_containers.py::test_ecs_task_exit_code_and_stop` | `RunTask` exit code, `StopTask` with `stoppedReason` (Docker-backed, no Fargate isolation) |
| RDS (PostgreSQL) | `...test_floci_containers.py::test_rds_postgres_create_connect_query_delete` | Create, connect through the proxy port, query, delete |
| Lambda + API Gateway | `...test_floci_containers.py::test_lambda_invoke_and_api_gateway_http_and_rest` | Python function invoke; HTTP API (v2) and REST API (v1) proxy integrations |
| Terraform | [`infra/terraform/local`](../../infra/terraform/local) | `init -backend=false`, `validate`, `fmt -check` only; never applied |

Final per-test results, timings and cleanup evidence are recorded in
[../testing/S2115-FLOCI-INTEGRATION-REPORT.md](../testing/S2115-FLOCI-INTEGRATION-REPORT.md).

**What local proof does not show:** real IAM enforcement, VPC/security-group behaviour, Fargate
isolation, KMS, quotas, latency, cost, regional failure, or anything about the real AWS account,
organisation or network. Floci is an emulator; a passing test proves the API shape and our client
code, not AWS behaviour.

## 2. Service-to-component map and why each component stays unchanged today

| AWS service | Component it could serve | Today | Why it stays unchanged |
|---|---|---|---|
| SQS (+ DLQ) | PostgreSQL job queue (`persistence/repositories/job_queue.py`) | PostgreSQL `jobs` table | The intake job is inserted in the same transaction as the webhook receipt (ADR C1); leases, `SKIP LOCKED`, dedupe and crash recovery are tested. SQS would be a redesign with a migration or a ledger, not a config switch |
| S3 | Artifact store (`application/artifacts/ports.py`, `execution/artifacts.py`) | `LocalFilesystemArtifactStore` under `ARTIFACT_ROOT` | Needs an `S3ArtifactStore` adapter with never-overwrite and re-hash semantics, and a copy of existing artifacts |
| ECS / Fargate (or Lambda) | Docker sandbox (`execution/docker/*`) | `docker run` on the worker's host daemon | The sandbox is the security boundary (ADR C2, ADR-010). A new backend needs a full security re-review: no network, read-only root, limits, no credentials |
| ECR | Execution image `execution-python` | Built locally with `docker build` | Only relevant once sandboxes run outside the worker host |
| RDS (PostgreSQL) | Compose PostgreSQL (`acca-postgres`) | PostgreSQL 16 in Compose | Only `DATABASE_URL` would change, but the hosting decision for staging/production is not made |
| Secrets Manager | GitHub App key, Jira token, webhook secret, LangSmith key | `.env` and a 0600 PEM file read by the worker | Needs a secret source for `Settings` and an IAM design; not approved |
| CloudWatch Logs / Metrics | Worker and API logs, cost metrics | structlog JSON to stdout; cost ledger in PostgreSQL | Needs a handler or agent sidecar; not approved |
| Lambda + API Gateway | Webhook intake (`acca-api`) | FastAPI container behind ngrok | The API is small and stateless; a move is a deployment choice, not needed now |
| IAM / STS | Credentials for all of the above | Bedrock uses the worker's standard AWS chain | No direct calls beyond Bedrock |
| Bedrock Runtime | LLM provider (`providers/llm/factory.py`) | Anthropic (dev/test), Bedrock (staging/production) | Floci's Bedrock is a stub or proxy only; real model calls are out of scope. Not pointed at Floci |

## 3. What a real deployment still needs (needs approval)

None of these is implemented. Each is an architecture or tooling decision for the owner:

1. **SQS replacing the PostgreSQL job queue** (`jobs` table, leases, `SKIP LOCKED`, dedupe, crash tests;
   would need an Alembic migration or a ledger redesign).
2. **S3 replacing filesystem artifacts** (new `ArtifactStore` adapter, never-overwrite semantics, copy of
   existing `ARTIFACT_ROOT` data).
3. **ECS (or Lambda) replacing the Docker sandbox** (security re-review of ADR C2 controls).
4. **RDS replacing the Compose PostgreSQL** for any environment.
5. **Secrets Manager as the source for GitHub/Jira/webhook secrets.**
6. **CloudWatch as a log/metric sink** for the worker or API.
7. **Pointing the Bedrock provider at Floci's Bedrock Runtime stub** (endpoint override in
   `providers/llm/factory.py`).
8. **Any `src` AWS client factory or new `Settings` fields**, which needs `test_sdk_imports_are_confined`
   (`tests/unit/test_architecture.py`) widened beyond `providers/llm/factory.py`.
9. **Adopting Terraform as the IaC tool** and writing real-AWS modules (remote state, IAM, VPC, KMS).
   Terraform 1.17.2 was installed locally only to run `validate` on `infra/terraform/local`; real-AWS
   modules are deferred to the AWS deployment-preparation milestone.
10. **Confirming the wording of the Docker-socket exception** in ADR-014, which amends the
    `docs/ARCHITECTURE.md` guidance for this one local, test-only container.
11. **Enabling `FLOCI_SERVICES_IAM_ENFORCEMENT_ENABLED`, `FLOCI_TLS_ENABLED` or
    `FLOCI_SERVICES_ECS_TASK_ROLE_CREDENTIALS_ENABLED`** (not needed for the tests; they change Floci behaviour).

Beyond those decisions, a real deployment also needs: an AWS account and region, an IAM model
(roles for the worker, API and CI; no long-lived keys), networking (VPC, subnets, egress control for the
dependency resolver and documentation fetcher), KMS keys, a worker host that satisfies the
[future worker deployment](../ARCHITECTURE.md#future-worker-deployment) options, backups and
monitoring, and a cost estimate. None of these can be proven on Floci.

## 4. Floci gaps to remember

See the [capability matrix](FLOCI-SERVICE-CAPABILITY-MATRIX.md). In short: IAM not enforced by default;
ECS has no Fargate isolation and task-role credentials are off; RDS honours `Port` only inside the
proxy range; Lambda `InvokeWithResponseStream` and layer permissions are absent; API Gateway v1
authorizer testing, model templates, documentation versions and client certificates are absent;
STS session tags are not modelled; Bedrock Runtime is a stub or proxy.
