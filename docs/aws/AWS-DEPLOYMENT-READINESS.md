# AWS deployment readiness (S2115, S2116)

Status: 2026-11-27. Branch `feature/dynamodb-aws-alignment`.

- **S2115** added [Floci](https://github.com/floci-io/floci) 2.5.0 as a local AWS emulator and tests of the
  AWS service shapes a deployment could use ([ADR-014](../decisions/ADR-014-floci-local-aws-emulator.md)).
- **S2116** moved the application onto AWS-native persistence: DynamoDB is the only database, jobs are
  DynamoDB state with SQS delivery and a DLQ, LangGraph checkpoints use `DynamoDBSaver` with S3 offload,
  artifacts are write-once S3 objects, and `bedrock` can run locally through the Floci proxy
  ([ADR-015](../decisions/ADR-015-dynamodb-sqs-s3-persistence.md)). DynamoDB started empty; nothing was
  migrated.

Related: [DYNAMODB-DATA-MODEL.md](DYNAMODB-DATA-MODEL.md) ·
[DYNAMODB-ACCESS-PATTERNS.md](DYNAMODB-ACCESS-PATTERNS.md) ·
[S2116 implementation audit](S2116-IMPLEMENTATION-AUDIT.md) ·
[S2115 architecture audit](S2115-ARCHITECTURE-AUDIT.md) ·
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
| RDS (PostgreSQL) | `...test_floci_containers.py::test_rds_postgres_create_connect_query_delete` | Create, connect through the proxy port, query, delete (emulator proof only; the application does not use RDS) |
| Lambda + API Gateway | `...test_floci_containers.py::test_lambda_invoke_and_api_gateway_http_and_rest` | Python function invoke; HTTP API (v2) and REST API (v1) proxy integrations |
| DynamoDB application store (S2116) | `tests/integration/floci/test_*_dynamo.py`, `test_dynamo_foundations.py`, `test_dynamo_call_counts.py` | Every store adapter on the real table schema: conditions, transactions with fresh client tokens, GSIs, TTL attributes, pagination; call counts of the hot paths pinned |
| Job model (S2116) | `test_job_queue_dynamo.py`, `test_worker_crash_dynamo.py` | Outbox relay and sweep, conditional claim, lease and heartbeat, retry by visibility, DLQ, worker crash; a 100-iteration chaos test without double execution |
| Checkpoints (S2116) | `test_checkpointer_dynamo.py` | `DynamoDBSaver` 1.3.0 conformance, S3 offload at the 358,400-byte boundary and for states over 400 KB, resume on a fresh worker, TTL and lifecycle rule |
| Artifacts (S2116) | `test_s3_artifacts.py` | Write-once `IfNoneMatch="*"`, re-hash on read, tamper detection, listing across pages |
| Composition and CLI (S2116) | `test_composition_floci.py`, `test_cli_dynamo.py`, `test_aws_clients_floci.py` | API and worker built on Floci; operator commands; endpoint guard and dummy credentials |
| Bedrock via the Floci proxy (S2116) | `test_bedrock_proxy.py` (fake upstream) and a 5-call owner-approved canary | Converse text, tool use, tool results, multi-turn, structured output; ledger provider `bedrock-floci` ([LLM-COST-TRACKING.md](../LLM-COST-TRACKING.md#bedrock-through-floci-local-only-provider-bedrock-floci)) |
| Terraform | [`infra/terraform/local`](../../infra/terraform/local) | `init -backend=false`, `validate`, `fmt -check` only; never applied |

Final per-test results, timings and cleanup evidence are recorded in
[../testing/S2115-FLOCI-INTEGRATION-REPORT.md](../testing/S2115-FLOCI-INTEGRATION-REPORT.md).

**What local proof does not show:** real IAM enforcement, VPC/security-group behaviour, Fargate
isolation, KMS, quotas, latency, cost, regional failure, or anything about the real AWS account,
organisation or network. Floci is an emulator; a passing test proves the API shape and our client
code, not AWS behaviour.

## 2. Service-to-component map

| AWS service | Component | Today (S2116) | Remaining for a real deployment |
|---|---|---|---|
| DynamoDB | All application state, registry, usage ledger, job state and outbox; LangGraph checkpoints | **In use**: two on-demand tables (`infra/floci/schema/*.json`), `persistence/dynamodb/`, `orchestration/langgraph/checkpointer.py` | Create the tables in the account (IaC), PITR and deletion protection on the application table, encryption, IAM |
| SQS (+ DLQ) | Job delivery (`persistence/dynamodb/job_queue.py`) | **In use**: standard queue, `maxReceiveCount` 20 to the DLQ, relay and sweep in the worker | Queue and DLQ in the account, DLQ depth alarm, IAM |
| S3 | Artifacts (`execution/s3_artifacts.py`) and checkpoint offload | **In use**: `artifacts/` (write-once) and `checkpoints/` (lifecycle expiry) | Bucket with Block Public Access and encryption, IAM (no delete on `artifacts/`), decision on cleaning orphan blobs |
| Bedrock Runtime | LLM provider (`providers/llm/factory.py`) | Anthropic (dev/test); `bedrock` via the Floci proxy (dev/test); real Bedrock (staging/production) | Model access, a real model or inference-profile id, `LLM_PRICING_FILE` with Bedrock prices, streaming/caching review |
| ECS / Fargate (or Lambda) | Docker sandbox (`execution/docker/*`) | `docker run` on the worker's host daemon | Security re-review of the sandbox (ADR C2, ADR-010): no network, read-only root, limits, no credentials |
| ECR | Execution image `execution-python` | Built locally with `docker build` | Only relevant once sandboxes run outside the worker host |
| Secrets Manager | GitHub App key, Jira token, webhook secret, LangSmith key | `.env` and a 0600 PEM file read by the worker | A secret source for `Settings` and an IAM design; not approved |
| CloudWatch Logs / Metrics | Worker and API logs, metrics | structlog JSON to stdout | A handler or agent sidecar; not approved |
| Lambda + API Gateway | Webhook intake (`acca-api`) | FastAPI container behind ngrok | A deployment choice, not needed now |
| IAM / STS | Credentials for all of the above | Default credential chain in staging/production; dummy `test`/`test` locally | Roles for API, worker, CI and operators: [SECURITY.md](../SECURITY.md#aws-access-iam) |
| RDS | None | Not used: ADR-015 removed PostgreSQL from the application | - |

## 3. What a real deployment still needs (needs approval)

1. **The AWS account, region and IaC** for the two tables, the queue and DLQ and the bucket (the
   validate-only Terraform in `infra/terraform/local` covers the local resources only; real-AWS modules are
   deferred to the deployment milestone).
2. **IAM roles** per process as in [SECURITY.md](../SECURITY.md#aws-access-iam): the API only on the
   application table; the worker on both tables, the queue and the bucket, including
   `s3:GetLifecycleConfiguration`/`s3:PutLifecycleConfiguration` for the checkpoint saver and no
   `s3:DeleteObject` on `artifacts/`.
3. **Real Bedrock:** model access, the model or inference-profile id and its prices (`LLM_PRICING_FILE`).
   Streaming, prompt caching and error mapping through Bedrock are unverified.
4. **ECS (or Lambda) replacing the Docker sandbox** (security re-review of ADR C2 controls).
5. **Secrets Manager as the source for GitHub/Jira/webhook secrets.**
6. **CloudWatch as a log/metric sink**, including the DLQ depth alarm.
7. **Production table settings:** PITR and deletion protection (application table), encryption at rest, the
   billing mode review once real Story volume is known (DYNAMODB-DATA-MODEL.md section 13).
8. **Confirming the wording of the Docker-socket exception** in ADR-014.
9. **Enabling `FLOCI_SERVICES_IAM_ENFORCEMENT_ENABLED`, `FLOCI_TLS_ENABLED` or
   `FLOCI_SERVICES_ECS_TASK_ROLE_CREDENTIALS_ENABLED`** (not needed for the tests; they change Floci behaviour).

Beyond those decisions, a real deployment also needs networking (VPC, subnets, VPC endpoints for DynamoDB,
SQS and S3, egress control for the dependency resolver and documentation fetcher), KMS keys, a worker host
that satisfies the [future worker deployment](../ARCHITECTURE.md#future-worker-deployment) options, backups,
monitoring and a cost estimate. None of these can be proven on Floci.

## Cost (summary)

DynamoDB on-demand, `STANDARD` table class, three sparse GSIs with minimal projections. Hot items stay under
1 KB (one write unit). Idle cost per worker with the default intervals (relay 30 s, sweep 300 s) is about
1,584 read request units a day plus SQS long polls (about 4,320 `ReceiveMessage` per claim slot a day).
TTLs: terminal jobs 90 days, Jira delivery records 30 days, checkpoints 30 days (item TTL plus S3 lifecycle).
The full reasoning, with request-unit counts instead of prices, is in
[DYNAMODB-DATA-MODEL.md](DYNAMODB-DATA-MODEL.md) section 13.

## 4. Floci gaps to remember

See the [capability matrix](FLOCI-SERVICE-CAPABILITY-MATRIX.md). In short: IAM not enforced by default;
ECS has no Fargate isolation and task-role credentials are off; RDS honours `Port` only inside the
proxy range; Lambda `InvokeWithResponseStream` and layer permissions are absent; API Gateway v1
authorizer testing, model templates, documentation versions and client certificates are absent;
STS session tags are not modelled; Bedrock Runtime is a stub or proxy (the `proxy` backend is used locally
by ADR-015). For the application's services (DYNAMODB-DATA-MODEL.md section 10.4): IAM is not enforced; GSIs are
immediately consistent; `TransactionConflict` and throttling never occur (covered by `Stubber` tests);
stale receipt handles and visibility timeouts over 43,200 s are accepted; TTL is swept every 60 s; the
Bedrock proxy has no prompt caching and no `InvokeModel`.
