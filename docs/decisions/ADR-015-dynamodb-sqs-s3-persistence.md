# ADR-015: DynamoDB, SQS and S3 as the application's persistence

Status: accepted (2026-11-27, owner-approved in S2116). **Supersedes** decisions C1 and C8 of
[ARCHITECTURE-DECISIONS.md](../ARCHITECTURE-DECISIONS.md). **Extends**
[ADR-014](ADR-014-floci-local-aws-emulator.md): Floci is no longer test-only, it is the local AWS endpoint
of the API and the worker in development and test.

Design: [DYNAMODB-DATA-MODEL.md](../aws/DYNAMODB-DATA-MODEL.md) ·
[DYNAMODB-ACCESS-PATTERNS.md](../aws/DYNAMODB-ACCESS-PATTERNS.md) ·
[S2116-IMPLEMENTATION-AUDIT.md](../aws/S2116-IMPLEMENTATION-AUDIT.md) ·
[AWS-DEPLOYMENT-READINESS.md](../aws/AWS-DEPLOYMENT-READINESS.md).

## Context

The application kept all state in one PostgreSQL instance: domain tables, a job queue claimed with
row locks (C1) and LangGraph checkpoints in their own schema (C8). The owner chose AWS-native
persistence for the AWS deployment, built and tested locally against the Floci 2.5.0 emulator, without
disturbing the live runtime that keeps serving Jira from the main checkout.

## Decision

- **Fresh start, no migration.** DynamoDB starts empty. No data is copied from PostgreSQL, there is no
  dump, no dual-write period, no backend flag or switch-over setting and no PostgreSQL fallback. The
  composition switched in one cut; PostgreSQL then left the code, the dependencies, `compose.yaml` and
  the tests. The historical schema migrations were deleted (git history keeps them). An existing
  `acca-postgres` container and its volume are left alone: nothing in this branch uses or removes them.
- **DynamoDB is the only application database.** Two on-demand tables, `STANDARD` class:
  - the application table (`ACCA_DYNAMODB_TABLE_NAME`): every entity the application owns (requests,
    attempts, approvals, multi-repository parents and children, the registry and its audit events, the
    usage ledger, Jira delivery records, jobs, uniqueness guards and the outbox) in item collections with
    key prefixes; invariants that used to be constraints and triggers are condition expressions and
    `TransactWriteItems`; three sparse GSIs with minimal projections;
  - the checkpoint table (`ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME`), whose schema belongs to the checkpoint
    library.

  Schemas live in `infra/floci/schema/*.json` and are created by `sh infra/floci/init/bootstrap.sh`
  (idempotent; it refuses an index that drifted from the schema). There are no migrations.
- **Job model.** DynamoDB holds the authoritative job state; an SQS **standard** queue with a DLQ
  (`maxReceiveCount` 20) only delivers `{"job_id"}`. The job item is the transactional outbox row: it is
  written in the same `TransactWriteItems` as the domain change (for intake: the Jira delivery, the
  request and the job), with a sparse GSI3 entry in `DISPATCH`. Worker-side producers send right after
  commit; the **dispatcher** (outbox relay every `JOB_RELAY_SECONDS`, lost-message sweep every
  `JOB_SWEEP_SECONDS`) is a loop inside the existing worker process, not a new service. Every receipt
  goes through a conditional claim with a lease, so duplicate or late messages are no-ops. **The API
  never calls SQS**: its intake jobs are sent by the relay (intake latency at most one relay interval).
- **Checkpointer.** `DynamoDBSaver` from `langgraph-checkpoint-aws==1.3.0` (exact pin), with
  `consistent_read=True` and mandatory S3 offload: payloads over the library's 358,400-byte threshold go
  to the `checkpoints/` prefix of the application bucket. Retention `ACCA_CHECKPOINT_TTL_DAYS` (default 30)
  is both the item TTL and an S3 lifecycle rule the saver installs.
- **Artifacts in S3.** `S3ArtifactStore` (`execution/s3_artifacts.py`) under the `artifacts/` prefix:
  write-once (`PutObject` with `IfNoneMatch="*"`), re-hashed on every read, same size limits. Workspaces
  (`WORKSPACE_ROOT`) and dependency bundles (`ARTIFACT_ROOT/dependency-bundles`) stay on local disk because
  they are bind-mounted into sandboxes.
- **One AWS client factory.** `integrations/aws/clients.py` (`AwsClients`) builds every DynamoDB, SQS and
  S3 client from `ACCA_*` settings. The standard `AWS_*` variables never configure the application.
- **Local endpoint guard.** An endpoint override (`ACCA_AWS_ENDPOINT_URL`, `ACCA_BEDROCK_ENDPOINT_URL`)
  is accepted only when `APP_ENV` is development or test, the scheme is `http`, and the host is loopback
  or a single-label Docker host name (`acca-floci`); any dotted name (so any `amazonaws.com`,
  `amazonaws.com.cn` or `api.aws` host) is refused with `ConfigurationError`. Development and test
  **require** the services endpoint, so they never fall through to real AWS. With an override the clients
  use the static credentials `test`/`test` and `ignore_configured_endpoint_urls=True`; they never read
  the default credential chain, `AWS_PROFILE` or `AWS_ENDPOINT_URL*`. Staging and production must not set
  an override and use the process's default credential chain (an IAM role).
- **LLM modes.** `anthropic` (unchanged, development and test) | `bedrock` through the Floci Bedrock
  Runtime `proxy` backend to Anthropic's OpenAI-compatible endpoint (`ACCA_BEDROCK_ENDPOINT_URL`, local
  only; ledger provider `bedrock-floci`) | real Bedrock (`LLM_PROVIDER=bedrock` without override; the only
  provider in staging and production). No LiteLLM.
- **Networking.** Host processes use `http://127.0.0.1:4566`; containers join the external `acca-floci`
  network and use `http://acca-floci:4566`, never `localhost`.

## Consequences

- **No ad hoc SQL.** Every read is a designed access pattern (key, GSI or batch get); new queries need a
  key design first. Listings read keys from slim GSIs and fetch the items (more read units per listing,
  fewer index writes per transition). Display-only reads may be eventually consistent; anything that
  decides is strongly consistent.
- **Delivery is at least once.** Handlers stay idempotent; the conditional claim, not the message, decides
  ownership. Claim order is best effort. Poison messages land in the DLQ; DynamoDB still says what happened.
- **Idle cost** comes from polling: about 1,584 read request units per worker per day with the default
  intervals, plus SQS long polls (DYNAMODB-DATA-MODEL.md section 13).
- **Local proof is not AWS proof.** Floci does not enforce IAM, visibility-timeout limits or
  throughput; the job and checkpoint semantics are tested on Floci and with botocore `Stubber` for
  AWS-only errors (transaction conflicts, throttling, stale receipts).
- **IAM is part of the deployment.** API: the application table only (no SQS, no S3). Worker: both
  tables, the queue and its DLQ, the bucket (`artifacts/` without delete; `checkpoints/` including the
  lifecycle configuration), and Bedrock. See [SECURITY.md](../SECURITY.md#aws-access-iam).
- **Orphans.** An interrupted artifact write can leave an S3 blob without its metadata object; it is
  invisible to readers and nothing deletes it (the store holds no delete permission).
- **The live checkout keeps PostgreSQL** until this branch is merged and deployed. From this branch, never
  run `docker compose ... --remove-orphans` on project `ai-code-change-agent`: it would remove the
  `acca-postgres` container that the live runtime still uses.

## Bedrock through Floci: canary (2026-11-27, owner-approved)

Five real calls with `claude-haiku-5-5` through a throwaway Floci (`proxy` backend, documented
`FLOCI_SERVICES_BEDROCK_RUNTIME_*` variables only) using the application's own `ChatBedrockConverse`
factory: a simple reply, a tool call, a tool-result round trip, a two-turn conversation and structured
output all passed; 1,551 tokens, about $0.0002 (cap $0.10); the usage ledger rows (provider
`bedrock-floci`) matched. **Not verified:** streaming (`ConverseStream`), prompt caching (the
OpenAI-compatible path reports no cache tokens), stop-reason and error/throttle mapping, and real Bedrock
model ids (`us.anthropic.claude-haiku-5-5-v1:0` was only a mapping key).

## Rejected alternatives

- **Migrating PostgreSQL data, dual-write or a backend flag:** owner ruled out; DynamoDB starts empty.
- **One DynamoDB table per PostgreSQL table:** the change aggregate is read and written together under one
  optimistic lock; one item collection does that cheapest.
- **RDS:** keeps the operational model the owner chose to leave.
- **SQS FIFO:** its 5-minute deduplication window cannot replace permanent dedupe; nothing needs ordering.
- **DynamoDB Streams with Lambda or EventBridge Pipes as the outbox:** more moving parts, no help with
  retries; on Floci stream records are in memory only.
- **Direct `SendMessage` from the API:** a crash between commit and send loses the job, and the API would
  need SQS permissions.
- **LiteLLM between Floci and Anthropic:** an extra container holding the key; the direct proxy proved
  sufficient.
- **`aioboto3`, a custom DynamoDB fake, Floci init hooks, Testcontainers:** not needed; tests run against the
  pinned Floci instance and `Stubber` (S2116 audit 9.1 and 9.2).
