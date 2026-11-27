# S2116 implementation audit: DynamoDB, SQS, S3 and Bedrock on Floci

Status: audit before implementation (2026-11-27), branch `feature/dynamodb-aws-alignment` at `2bf3bdd`.
Read-only: no source, configuration or runtime was changed to write it. Design:
[DYNAMODB-ACCESS-PATTERNS.md](DYNAMODB-ACCESS-PATTERNS.md) and [DYNAMODB-DATA-MODEL.md](DYNAMODB-DATA-MODEL.md).
Floci references: the official documentation at tag `2.5.0` (`github.com/floci-io/floci`, `docs/**`) and
`https://floci.io/floci/` (services, environment variables, initialization hooks, Testcontainers). Where the
two differ, the 2.5.0 tag wins because that is the pinned image.

Owner decisions this audit applies: DynamoDB is the only application database and starts empty (no data
migration, no `pg_dump`, no dual-write, no backend flag, no PostgreSQL fallback); SQS with a DLQ delivers
jobs, with a DynamoDB outbox relayed by a dispatcher role inside the existing worker deployment;
`DynamoDBSaver` from `langgraph-checkpoint-aws==1.3.0` with S3 offload; S3 for artifacts; LLM modes
`anthropic`, `bedrock` through the Floci Bedrock Runtime proxy, and real Bedrock; PostgreSQL leaves the
active runtime at the end. Secrets Manager, CloudWatch, ECR and ECS stay out of scope.

## 1. Existing architecture

| Part | Where it runs | Persistence today |
|---|---|---|
| API (FastAPI: Jira webhook intake, health) | Container `acca-api` from `compose.yaml` (project `ai-code-change-agent`, network `ai-code-change-agent_default`, port `127.0.0.1:8000`, read-only root, no capabilities) | `DATABASE_URL` to `acca-postgres` |
| Worker (job loop, LangGraph workflow, Deep Agent, validation, delivery) | Host process `uv run ai-code-change worker`, under `caffeinate` (it drives the Docker daemon for sandboxes) | PostgreSQL domain tables and LangGraph checkpoints (schema `langgraph`); artifacts on the local filesystem |
| Execution sandboxes | Sibling containers `aica-sbx-*`, labelled `aica.managed`, `aica.attempt`, `aica.workspace`, `aica.role` | Workspaces and dependency bundles on the host |
| PostgreSQL 16 | Container `acca-postgres`, volume `acca_pgdata`, `127.0.0.1:5435` | Everything above |
| Floci 2.5.0 (S2115) | Separate project `acca-floci`, network `acca-floci`, `127.0.0.1:4566`, pinned by digest | Used only by the `floci` tests today; no application code talks to it |
| ngrok | Host process forwarding to `127.0.0.1:8000` | - |

Live runtime observed on 2026-11-27: `acca-api`, `acca-postgres`, the host worker (started 13:38) and ngrok
are running and serve Jira. They must keep running during S2116 (section 8).

## 2. Persistence dependencies

| Area | Files | Depends on |
|---|---|---|
| SQL adapters | `persistence/repositories/{webhook_repository, change_store, job_queue, multirepo_store, registry_store, usage_store}.py` | SQLAlchemy async sessions |
| Models and schema | `persistence/models/*.py`, `persistence/migrations/**` (4 Alembic revisions with triggers), `alembic.ini`, `persistence/session.py` | SQLAlchemy, Alembic, psycopg |
| Composition | `composition.py` (`require_database`, `create_engine`, `SqlChangeUnitOfWork`, `PostgresJobQueue`, `PostgresMultiRepoStore`, `PostgresUsageStore`, `SqlAlchemyRegistryUnitOfWork`, `SqlAlchemyWebhookRepository`, `postgres_checkpointer`), `main.py`, `cli/main.py`, `cli/operations.py` | All adapters |
| Readiness | `api/routes/health.py` (database probe) | SQLAlchemy |
| Checkpointer | `orchestration/langgraph/checkpointer.py` | `langgraph-checkpoint-postgres`, psycopg |
| Studio (dev only) | `studio/history.py`, `studio/live.py` (raw SQL on attempts, usage and checkpoints; read-only `AsyncPostgresSaver`) | SQLAlchemy, psycopg |
| Settings | `core/config.py` (`database_url`, `langgraph_checkpoint_schema`) | - |
| Deployment | `compose.yaml` (`postgres` service, API `DATABASE_URL`), `docker/api/Dockerfile` (locked deps) | - |
| Tests | Marker `integration` (`tests/integration/*_db.py`, `test_registry_database.py`, `test_webhook_repository.py`, `test_crash_recovery.py`), unit tests that import SQL modules (`test_persistence_and_errors.py`, `test_settings.py`, `api/test_health.py`, `api/test_app_lifecycle.py`, architecture tests), coverage omit for migrations | PostgreSQL on `127.0.0.1:5435` |
| Dependencies | `pyproject.toml`: `sqlalchemy[asyncio]`, `psycopg[binary,pool]`, `alembic`, `langgraph-checkpoint-postgres`; `boto3` is dev-only | - |
| Instructions | `CLAUDE.md`, `AGENTS.md`, `README.md`, `docs/GETTING-STARTED.md`, `docs/CONFIGURATION.md` (`alembic upgrade head`) | - |

The ports already isolate the domain: `ChangeUnitOfWork`, `RegistryUnitOfWork`, `JobQueue`, `MultiRepoStore`,
`UsageStore`/`CostQuery`, `WebhookRepository` and `ArtifactStore`. Studio is the only code that bypasses them.

## 3. Job queue today

`PostgresJobQueue` (`persistence/repositories/job_queue.py`) claims with `FOR UPDATE SKIP LOCKED`, leases
300 s with a 60 s heartbeat, retries with backoff of 30 s doubling to 900 s, reaps exhausted leases, and
deduplicates by a permanent unique `dedupe_key` (ADR C1). Producers: webhook intake (atomic with the
request), the multi-repository coordinator (child and report jobs), the CLI (resume after approval). The
`Worker` (`application/jobs/worker.py`) polls every 2 s with concurrency 2.

## 4. Checkpointer today

`AsyncPostgresSaver` in schema `langgraph`, set up at worker start; threads `attempt:<aid>` (workflow) and
`attempt:<aid>:<phase>:...` (Deep Agent). 948 checkpoints for 19 attempts; largest blob 1,246,727 bytes.
Replacement verified by the designer on Floci (data model section 9.3). The 1.3.0 offload threshold is a
fixed 358,400 bytes per payload, written as its own chunk item, so the 400 KB item limit has about 50 KB of
headroom for keys and TTL (data model section 9.2).

## 5. Artifacts today

`LocalFilesystemArtifactStore` (`execution/artifacts.py`) behind the `ArtifactStore` port: write-once,
SHA-256 verified, per-artifact and per-attempt byte limits. Studio reads `*.meta.json` from disk for the PR
link. Dependency bundles and workspaces are bind-mounted into sandboxes and stay local.

## 6. LLM factory today

`providers/llm/factory.py`: `anthropic` (`ChatAnthropic`, development and test only) and `bedrock`
(`ChatBedrockConverse` with region and optional profile from the standard AWS chain). No endpoint override
exists. The architecture test confines `langchain_aws`, `boto3` and `botocore` to this file.

## 7. Floci services available (official docs, 2.5.0)

Operation counts are from the floci.io services page; variables and caveats from the 2.5.0 docs.

| Need | Floci 2.5.0 | Caveats that shape the design |
|---|---|---|
| DynamoDB | 54 operations incl. `TransactWriteItems`, conditions, GSIs, TTL, Streams (4 operations) | GSIs immediately consistent; no `TransactionConflict`; TTL swept every 60 s; storage `FLOCI_STORAGE_SERVICES_DYNAMODB_ITEM_LAYOUT=per-table` and `FLOCI_STORAGE_SERVICES_DYNAMODB_MODE=wal` documented in `docs/configuration/storage.md:72-74` |
| SQS | 23 operations, DLQ redrive, `ChangeMessageVisibility`, long polling, `DelaySeconds` 0-900 | Stale receipt handles and visibility over 43,200 s are accepted (AWS rejects them); `QueueUrl` embeds `FLOCI_BASE_URL` (strategy `floci`), both URL forms accepted as input |
| S3 | 58 operations | `If-None-Match: *` works but is undocumented (probe-verified) |
| Bedrock Runtime | `stub` (default) and `proxy` backends; proxy variables `FLOCI_SERVICES_BEDROCK_RUNTIME_{BACKEND, PROXY_URL, PROXY_API_KEY, PROXY_DEFAULT_MODEL, PROXY_MODEL_MAPPING, PROXY_PASSTHROUGH, PROXY_ERROR_BODY_TIMEOUT_MILLIS}` (`docs/services/bedrock-runtime.md:34-41`) | Stub never returns tool use; proxy supports Converse/ConverseStream with tools, drops `cachePoint`; `InvokeModel` refused by the proxy |
| Container access | `FLOCI_HOSTNAME` overrides the host part of `FLOCI_BASE_URL` so other containers reach Floci by DNS | Containers join the `acca-floci` network and use `http://acca-floci:4566`; the host keeps `127.0.0.1:4566` |
| Web console | Sidecar started on first open of `/_floci/ui`; `FLOCI_SERVICES_UI_IMAGE` default `floci/floci-ui:0.7.0`, `FLOCI_SERVICES_UI_BIND_ADDRESS` unset publishes on every interface | **Running now as `floci-aws-ui` on `0.0.0.0:4500`**; pin the image by digest and bind to `127.0.0.1` |
| Initialization hooks | `/etc/floci/init/{boot,start,ready,stop}.d`, `FLOCI_INIT_HOOKS_DIR`, timeout 30 s; scripts that call `aws` need the `-compat` image; a failing start/ready hook shuts Floci down | See decision 9.1 |
| Testcontainers | `testcontainers-floci` 0.1.1, `FlociContainer` documented as wrapping `floci/floci:latest` | See decision 9.2 |

Variables seen on floci.io but absent from the 2.5.0 reference (for example
`FLOCI_SERVICES_DYNAMODB_TRANSACTION_CONFLICTS_ENABLED`) are not used.

## 8. Live runtime constraint: findings

1. **The live worker imports this checkout.** It runs `.venv/bin/ai-code-change` from
   `/Users/nishanth/Documents/tekion/ai-code-change-agent`, and the package is an editable install
   (`_editable_impl_ai_code_change_agent.pth`). Today the branch differs from `main` only in docs, infra,
   tests, `.env.example`, `README.md` and the dev `boto3` pin, so the worker effectively runs `main`. Any `src/` edit or `uv sync` in this
   checkout would reach the live worker on its next lazy import or restart. **Implementation must happen in a
   separate git worktree with its own `.venv`.**
2. `compose.yaml` hard-codes `container_name: acca-api` and port 8000 (the ngrok target). A branch API must run
   under another compose project, container name and port, never `docker compose up` on the default project.
3. Sandboxes are scoped by per-workspace labels; egress helpers by host and pid. A second worker on the same
   Docker daemon does not reap the live worker's containers, but the Docker integration tests' leftover guard
   fails while a live attempt's `aica-sbx-*` exists (project memory). Run those tests only when no live attempt is
   active.
4. The shared Floci (`acca-floci`) is not used by the live runtime; tests there use unique names. The final
   verification uses its own Floci instance on other ports.

## 9. Decisions taken in this audit

### 9.1 Bootstrap: host script, not Floci init hooks

Keep `infra/floci/init/bootstrap.sh` (host-run, idempotent, loopback and dummy-credential guard). Official hooks
would need the `2.5.0-compat` image (a new pinned digest), a 30 s per-script timeout, and a failure would shut
down the shared Floci. The host script also serves the tests. The script-internal init-directory and health-flag
variables are named `ACCA_FLOCI_INIT_DIR` and `ACCA_FLOCI_HEALTHY` (they used to carry a bare `FLOCI_` prefix,
which belongs to Floci: `FLOCI_INIT_HOOKS_DIR` is official), and the test helper's endpoint variable is
`ACCA_FLOCI_ENDPOINT_URL`.
Table definitions live in one JSON schema read by the script, Terraform and the test fixture.

### 9.2 Floci tests: running instance, not Testcontainers

`FlociContainer` is documented with `floci/floci:latest`; the project never runs `latest`. Keep the pinned compose
instance with skip-with-reason, unique resource names and cleanup. Revisit only if the module accepts a pinned
digest.

### 9.3 Application settings use the `ACCA_` prefix

New settings: `ACCA_AWS_REGION`, `ACCA_AWS_ENDPOINT_URL`, `ACCA_DYNAMODB_TABLE_NAME`,
`ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME`, `ACCA_SQS_JOB_QUEUE_NAME`, `ACCA_S3_BUCKET_NAME`,
`ACCA_BEDROCK_ENDPOINT_URL`. boto3 reads unprefixed `AWS_*` variables on its own, and `FLOCI_*` belongs to Floci,
so neither prefix is used for application settings. An endpoint override is allowed only in development and
test, only for `http` on a loopback address or a single-label Docker hostname (`acca-floci`), never for a host
under `amazonaws.com` or `api.aws`; with an override the client uses the static dummy credentials `test`/`test`
and never the default chain. Without an override (staging, production) the default chain (task role) applies.

### 9.4 PostgreSQL history

Delete `persistence/migrations/`, `alembic.ini`, models and SQL adapters in the removal task. No database is
upgraded from them any more and no data is migrated, so keeping them would be dead code needing exemptions
from the import guard; git history (commit `26ce3fd` and earlier) and the access-patterns document keep the
record. Volume `acca_pgdata` and the running `acca-postgres` stay untouched for the live runtime on `main`.

## 10. Replaced and unchanged

| Replaced | By |
|---|---|
| SQL adapters, models, Alembic, `session.py` | `persistence/dynamodb/*` behind the same ports |
| `PostgresJobQueue` | DynamoDB job state + SQS delivery + outbox dispatcher in the worker |
| `AsyncPostgresSaver` | `DynamoDBSaver` with S3 offload |
| `LocalFilesystemArtifactStore` for attempt artifacts | `S3ArtifactStore` |
| Database readiness probe | DynamoDB `DescribeTable` probe |
| Studio raw SQL | Ports (change store, cost query, artifact store) and a thread index item |
| `compose.yaml` `postgres` service and `DATABASE_URL` | API on the `acca-floci` network with DynamoDB settings only |

Unchanged: domain models and services (except a bounded retry on lost optimistic races), the LangGraph
workflow, the Deep Agent, the validation engine, Docker sandboxes and their hardening, GitHub and Jira
integrations, the webhook signature check, the `anthropic` LLM mode, cost pricing, the CLI commands.

## 11. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Editing or syncing the live checkout | Live worker breaks mid-run | Separate worktree and `.venv` (Task 0); never `uv sync` here |
| Job queue rewrite (claim, lease, visibility, outbox) | Lost or double-run jobs | Contract suite, crash tests, randomised kill/restart, `Stubber` for AWS-only errors |
| Optimistic locking replaces row locks | Spurious conflicts or lost updates | Version conditions on every dependent item, bounded retry, race tests |
| Floci more permissive than AWS (GSI consistency, conflicts, receipts, visibility cap, IAM) | Bugs hidden locally | No decision on a GSI; `Stubber` tests; Settings cap the lease |
| Checkpoint payloads over the item limit | Workflow fails | Mandatory S3 offload; threshold boundary tests |
| Third-party checkpointer upgrades | Behaviour drift | Exact pin; conformance suite in the `floci` tests |
| Endpoint override reaching real AWS or real credentials reaching Floci | Real writes or leaked keys | Settings guard (9.3); static dummy credentials with an override; tests never use ambient credentials |
| Floci proxy to a paid upstream | Unplanned spend | Tests use a local fake upstream; any real call needs explicit authorisation |
| UI sidecar on all interfaces | Local AWS state exposed on the LAN | `FLOCI_SERVICES_UI_BIND_ADDRESS=127.0.0.1` and a pinned image |
| Single cut to DynamoDB | No fallback if a gap appears late | Adapters contract-tested before the cut; full local E2E before Postgres removal |
