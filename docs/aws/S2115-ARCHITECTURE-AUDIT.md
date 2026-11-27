# S2115 architecture audit: AWS usage and the Floci local emulator

Status: draft (Phase 1, read-only audit). Branch `feature/s2115-floci-local-aws`, base `1f05bd9`.
Date: 2026-11-27. Nothing in this document has been run against Floci yet.

## 1. Scope and method

Read: `compose.yaml`, `docker/api/Dockerfile`, `src/ai_code_change_agent/core/config.py`,
`persistence/models/*`, `persistence/migrations/versions/*`, `persistence/repositories/job_queue.py`,
`application/artifacts/*`, `execution/artifacts.py`, `execution/docker/*`,
`providers/llm/factory.py`, `.env.example`, `pyproject.toml`, `tests/integration/conftest.py`,
`tests/unit/test_architecture.py`, `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE-DECISIONS.md`,
`docs/SECURITY.md`, `AGENTS.md`. Searched `src`, `tests` and `studio` for `boto`, `bedrock`, `aws`, `s3`, `sqs`.

## 2. Current architecture (as built)

| Concern | Implementation today | Where |
|---|---|---|
| Webhook intake API | FastAPI in Compose (`acca-api`), read-only root FS, `cap_drop: ALL`, loopback port `127.0.0.1:8000`, no Docker access, no GitHub/LLM/Jira REST credentials | `compose.yaml`, `docker/api/Dockerfile` |
| Database | PostgreSQL 16 in Compose (`acca-postgres`, `127.0.0.1:5435`, volume `acca_pgdata`). Domain tables in `public`, LangGraph checkpoints in schema `langgraph`. Alembic migrations `0001`-`0004` | `compose.yaml`, `persistence/` |
| Job queue | PostgreSQL `jobs` table: `SELECT ... FOR UPDATE SKIP LOCKED` claim, owner-checked leases, heartbeat, `DEAD` on exhausted attempts, `dedupe_key` unique | `persistence/repositories/job_queue.py`, `persistence/models/jobs.py` |
| Worker | Host process (`uv run ai-code-change worker`), trusted control plane, drives the host Docker daemon (ADR C7) | `cli/operations.py`, `application/jobs/worker.py` |
| Sandbox | `docker run` per command: `--network none`, non-root, read-only, cap-drop ALL, no socket, no host paths, resource limits (ADR C2) | `execution/docker/runtime.py`, `sandbox.py` |
| Dependency resolver | `--internal` Docker network `aica-dep-resolver` plus allow-list egress proxy (ADR-010) | `execution/docker/dependencies.py`, `resolver_network.py`, `egress_proxy.py` |
| Artifacts | `LocalFilesystemArtifactStore` under `ARTIFACT_ROOT`, immutable, content-hashed, redacted; behind the `ArtifactStore` port whose docstring already says "local filesystem today, S3 later". The DB stores only `artifact_sha256` on approvals | `application/artifacts/ports.py`, `execution/artifacts.py`, `persistence/models/changes.py` |
| LLM | `anthropic` (local/test) or `bedrock` (`ChatBedrockConverse`, staging/production only, enforced by Settings) | `providers/llm/factory.py`, `core/config.py` |
| Source control | GitHub App (installation-scoped) | `integrations/github/` |
| Issue tracker | Jira Cloud (webhook HMAC + read-only REST + optional single comment) | `integrations/jira/` |
| Tracing | LangSmith (optional, off by default) | `core/tracing.py` |

## 3. AWS services: integrated, planned, absent

| Service | State | Evidence |
|---|---|---|
| **Bedrock Runtime (Converse)** | **Integrated** (only AWS service in code). `langchain_aws.ChatBedrockConverse` with `BEDROCK_REGION`, optional `BEDROCK_CREDENTIALS_PROFILE`; credentials come from the worker's standard AWS chain. `test_sdk_imports_are_confined` pins `boto3`/`botocore`/`langchain_aws` imports to `providers/llm/factory.py` | `providers/llm/factory.py`, `tests/unit/test_architecture.py:32-36` |
| STS / IAM | Implicit only (the credential chain behind Bedrock). No direct calls | `factory.py` docstring |
| S3 | **Planned, not built** ("S3 later" in the artifact port docstring) | `application/artifacts/ports.py:1` |
| Compute for the worker (ECS / EC2 / microVM) | **Planned as options only** ("Future worker deployment": dedicated host/VM, remote sandbox service, rootless daemon) | `docs/ARCHITECTURE.md#future-worker-deployment` |
| RDS | Not referenced. Staging/production DB hosting is unspecified; `DATABASE_URL` is an opaque PostgreSQL URL | `core/config.py` |
| SQS, ECR, Lambda, API Gateway, Secrets Manager, CloudWatch | Not referenced anywhere | grep of `src`, `tests`, `studio` |
| Terraform / IaC | None in repo; `terraform` and `tofu` not installed on the host | `which terraform tofu` |

Host facts that matter for Floci (checked 2026-11-27): macOS 27.2 on `arm64`; Docker Desktop, Engine
29.9.2 `linux/arm64` with the **containerd image store** (`io.containerd.snapshotter.v1`);
`/var/run/docker.sock` symlinks to `~/.docker/run/docker.sock`; aws-cli 2.31.37; boto3/botocore
1.44.11 (transitive through `langchain-aws`, not a direct dependency).

## 4. Classification for S2115

### 4.1 Run locally on the Floci emulator (new, test-only, no app wiring)

S3, SQS (+ DLQ redrive), Secrets Manager, CloudWatch Logs and Metrics, ECR, ECS (Docker-backed
RunTask), RDS (PostgreSQL engine, container-backed), Lambda, API Gateway (REST v1 and HTTP v2), IAM, STS.
Purpose: prove the AWS shapes a future deployment would use, against a local emulator, with dummy
credentials, **without changing how the application runs today**.

### 4.2 Stay as they are (unchanged by S2115)

| Component | Why it stays |
|---|---|
| Compose PostgreSQL + Alembic | Source of truth for domain tables, the job queue and LangGraph checkpoints. Floci RDS is exercised only by an isolated test |
| PostgreSQL job queue | Leases, `SKIP LOCKED`, dedupe and crash recovery are tested (`test_worker_crash_db.py`, `test_crash_recovery.py`). SQS would be a redesign, not a config switch |
| Docker sandbox and resolver network | Security boundary (ADR C2, ADR-010). ECS is not a drop-in replacement and Floci itself needs the socket |
| Filesystem artifacts | The port exists; an S3 adapter is a behaviour change (see 4.4) |
| Bedrock / Anthropic providers | Real model calls are out of scope. Floci's Bedrock Runtime is a stub or proxy only (see capability matrix) |
| GitHub App, Jira Cloud, LangSmith | Not AWS |
| API container | Gets no AWS endpoint, no credentials, no socket |

### 4.3 Needs only configuration or test support (no business adapters)

- A pinned Floci service in its **own** Compose file and project, loopback-only ports, a named data volume, its own network.
- Idempotent host-run bootstrap scripts (aws CLI with `--endpoint-url`, loopback guard, `test`/`test` creds).
- A **test-only** boto3 client factory in `tests/support/`. Nothing in `src` needs a new AWS client, so
  no `Settings` field and no adapter is justified now. Putting a factory in `src` would also need the
  boto3 confinement rule in `tests/unit/test_architecture.py` to be widened (an architecture decision).
- A `floci` pytest marker (`--strict-markers` is on, so it must be registered in `pyproject.toml`).
- Note: `Settings` reads `.env` through pydantic-settings, which does **not** export values to
  `os.environ`. boto3 therefore never sees `AWS_ENDPOINT_URL` from `.env`. That is good: the worker's
  Bedrock client cannot be redirected by accident. It also means Floci variables are shell exports
  (or test fixtures), not `.env` settings. The worker must never run with `AWS_ENDPOINT_URL` exported,
  or the Bedrock client would follow it.

### 4.4 Would need adapters (architecture changes, not in S2115)

| Change | Adapter | Migration impact |
|---|---|---|
| S3 for artifacts | `S3ArtifactStore` implementing `ArtifactStore` (`put` must stay never-overwrite: `If-None-Match: *` or a HEAD check; `get` re-hashes) | No schema migration (DB keeps only hashes). Existing `ARTIFACT_ROOT` contents need a one-off copy |
| SQS for jobs | Replace `PostgresJobQueue` (claim/heartbeat/complete/fail map to receive / ChangeMessageVisibility / delete / DLQ) | Yes: `jobs` table semantics (dedupe, status, attempts, `DEAD`) move or are kept as a ledger; crash tests rewritten |
| ECS / Fargate for sandboxes | New `SandboxStarter`/`WorkspaceRuntime` backend | No DB migration; large security re-review (ADR C2: no network, read-only, limits, no credentials) |
| Secrets Manager for GitHub key / Jira token / webhook secret | Secret source for `Settings` | No DB migration |
| CloudWatch Logs/Metrics | structlog handler or agent sidecar | None |
| RDS for staging/production | None (it is PostgreSQL; only `DATABASE_URL`) | None beyond `alembic upgrade head` |
| Bedrock pointed at Floci's stub | Endpoint override in `providers/llm/factory.py` | None; but it touches the only AWS-confined module |

### 4.5 Database migrations

S2115 needs **no** Alembic migration. Only the SQS-replaces-jobs option would.

## 5. Security notes that shape the design

- `docs/ARCHITECTURE.md` says "Do not mount the Docker socket into a general-purpose container".
  Floci needs the socket for Lambda, RDS, ECS and ECR (`docs/configuration/docker-compose.md`,
  `docs/configuration/docker.md` in the Floci repo). Floci is a **local, test-only** emulator, so this is
  an explicit, documented exception: an ADR and a SECURITY.md row are part of the plan. The socket is
  never given to the API, the worker's sandboxes or any Floci-spawned workload.
- Floci-spawned containers (Lambda, ECS tasks, RDS) must not share a network with `acca-postgres` or
  `acca-api`. A separate Compose project with its own named network does this; an overlay on
  `compose.yaml` would put them on `ai-code-change-agent_default`.
- Bind every published port to `127.0.0.1`. Floci's container entrypoint listens on `0.0.0.0` inside
  the container (`-Dfloci.security.allow-unsafe-network-exposure=true`, `docker/entrypoint.sh`), so the
  loopback host binding is the only exposure control.
- Floci accepts any non-empty credentials (`docs/getting-started/aws-setup.md`). Scripts and tests pin
  `test`/`test`, unset `AWS_PROFILE`, point `AWS_CONFIG_FILE`/`AWS_SHARED_CREDENTIALS_FILE` at
  `/dev/null`, and refuse any endpoint that is not loopback, so a real AWS account is unreachable even
  if a developer has credentials configured.
- IAM enforcement is off by default (`FLOCI_SERVICES_IAM_ENFORCEMENT_ENABLED=false`). Tests must not
  claim IAM policy behaviour unless they turn it on.

## 6. Current vs proposed local AWS architecture

### Current

```
host
 ├─ compose project "ai-code-change-agent"  (network ai-code-change-agent_default)
 │   ├─ acca-postgres  127.0.0.1:5435   (domain tables, jobs queue, langgraph checkpoints)
 │   └─ acca-api       127.0.0.1:8000   (webhook intake, no docker, no AWS)
 ├─ worker (uv run, host)  ──► host Docker daemon ──► sandboxes (--network none)
 │                          ──► resolver network aica-dep-resolver (internal + proxy)
 │                          ──► Anthropic API (dev) | Bedrock (staging/prod)
 └─ ARTIFACT_ROOT / WORKSPACE_ROOT on local disk
```

### Proposed (S2115, additive)

```
host
 ├─ compose project "ai-code-change-agent"     UNCHANGED
 ├─ worker, sandboxes, artifacts, providers    UNCHANGED
 └─ compose project "acca-floci"  (infra/docker-compose.yml, network acca-floci)
     └─ acca-floci  floci/floci:2.5.0@sha256:<digest>
          ports 127.0.0.1:4566 (AWS APIs + ECR registry), 127.0.0.1:7001-7005 (RDS proxies)
          volume acca_floci_data:/app/data   (FLOCI_STORAGE_MODE=persistent)
          /var/run/docker.sock  (the ONLY container with the socket)
          spawns on network acca-floci: Lambda runtimes, ECS task containers,
                                         RDS postgres:16-alpine, registry:2 (ECR)
 pytest -m floci (host) ──► http://127.0.0.1:4566 with test/test creds
 infra/floci/init/bootstrap.sh (host aws CLI) ──► http://127.0.0.1:4566
```

## 7. Open risks to verify during implementation

1. **ECR on the containerd image store.** Floci documents that Engine 29+ with the containerd store treats
   `*.localhost` registries as HTTPS; use `FLOCI_SERVICES_ECR_URI_STYLE=path`
   (`localhost:4566/<account>/<region>/<repo>`) (`docs/services/ecr.md`, Emulation Behavior). Whether the
   Docker Desktop daemon reaches a `127.0.0.1`-bound host port as `localhost:4566` must be verified.
2. **RDS endpoint host** advertised from inside Docker: set `FLOCI_SERVICES_RDS_ENDPOINT_HOST` and verify
   `DescribeDBInstances` returns a host-reachable address and a port inside the published range.
3. **Lambda callback address**: Lambda containers must reach the Runtime API on the Floci container.
   Floci auto-detects; `FLOCI_HOSTNAME` / `FLOCI_SERVICES_LAMBDA_DOCKER_HOST_OVERRIDE` are the documented fallbacks.
4. **Image pulls** at first use (`public.ecr.aws/lambda/python:3.12`, `postgres:16-alpine`, `registry:2`,
   a small ECS task image). Tests must skip with a reason, not fail, when a pull is impossible offline.
5. **Clash with live sandboxes** on the same daemon (project memory): Floci's startup reconciliation
   only touches containers labelled as its own (`docs/configuration/docker.md`, Internal Labels), but
   run the heavy Floci tests when no agent run is active.
6. The Floci image digest for `2.5.0` was not resolved in this audit (no registry access); resolve it with
   `docker buildx imagetools inspect floci/floci:2.5.0` and pin `@sha256:`.
