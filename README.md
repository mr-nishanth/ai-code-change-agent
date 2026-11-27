# AI Code Change Agent

Turns a Jira Story into **draft pull requests**, for one repository or several. One LangGraph workflow, one Deep Agent and one independent Python validation engine. The coding agent works in an offline, hardened Docker sandbox, every change is validated independently, and nothing is merged automatically.

**Status (2026-11-26):** verified end to end with claude-haiku-5-5:
- KAN-7 and KAN-8: single repository;
- KAN-9: backend and client repositories, with the backend's API contract handed to the client.

## Components

| Component | Technology | Role |
|---|---|---|
| Agent API | Python / FastAPI | Jira webhook intake and health checks; never runs agents |
| Workflow | Python / LangGraph | One workflow per attempt, gates, retries |
| Coding agent | Python / Deep Agents | Plans and implements inside the sandbox |
| Execution sandbox | Python Docker image (`execution-python:0.2.0`) | Offline, hardened; runs **Python repositories only** |
| Validation | Python, independent of the agent | Fresh offline container; runs the profile's Python commands |
| Studio UI | React / TypeScript / Node.js (Vite) | Development UI to watch the agent; Node.js is a local development requirement only, not an execution toolchain |
| Persistence | AWS DynamoDB, SQS, S3 (locally the Floci emulator) | DynamoDB: requests, attempts, registry, job state, cost ledger, LangGraph checkpoints; SQS (+ DLQ): job delivery; S3: artifacts and large checkpoints ([ADR-015](docs/decisions/ADR-015-dynamodb-sqs-s3-persistence.md)) |

**Scope:** multi-repository means several registered **Python** repositories per Story (for example a backend API and a Python client, SDK or worker). Node.js, React/Next.js, Android, Kotlin/Java, Flutter and iOS/Swift repositories are out of scope.

## Start here

| Doc | What |
|---|---|
| [docs/PROJECT-FLOW.md](docs/PROJECT-FLOW.md) | How the whole flow works, Story to PR |
| [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) | Setup and run guide |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every environment variable |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/ARCHITECTURE-DECISIONS.md](docs/ARCHITECTURE-DECISIONS.md) | Architecture and ADRs |
| [docs/SECURITY.md](docs/SECURITY.md) | Security model |
| [docs/AUTOMATED-PIPELINE.md](docs/AUTOMATED-PIPELINE.md) | Automatic gates, retries, onboarding |
| [docs/MULTI-REPOSITORY.md](docs/MULTI-REPOSITORY.md) | Stories across several repositories |
| [docs/PYTHON-EXECUTION-AND-RESEARCH.md](docs/PYTHON-EXECUTION-AND-RESEARCH.md) | Python execution image, sandbox, documentation search |
| [docs/DYNAMIC-DEPENDENCY-PROVISIONING.md](docs/DYNAMIC-DEPENDENCY-PROVISIONING.md) | Offline dependency bundles |
| [docs/LLM-COST-TRACKING.md](docs/LLM-COST-TRACKING.md) | Cost ledger, reports, runaway-cost guards |
| [docs/DEEP-AGENT-STUDIO.md](docs/DEEP-AGENT-STUDIO.md) | React UI to watch the agent live |
| [docs/aws/DYNAMODB-DATA-MODEL.md](docs/aws/DYNAMODB-DATA-MODEL.md) · [docs/aws/AWS-DEPLOYMENT-READINESS.md](docs/aws/AWS-DEPLOYMENT-READINESS.md) | DynamoDB/SQS/S3 design, cost, AWS readiness |
| [docs/DOCKER-DEBUGGING.md](docs/DOCKER-DEBUGGING.md) | Inspecting containers, images, sandboxes and the Floci console |
| [docs/SETUP-GITHUB-APP.md](docs/SETUP-GITHUB-APP.md) · [docs/SETUP-JIRA-WEBHOOK.md](docs/SETUP-JIRA-WEBHOOK.md) · [docs/EXTERNAL-INTEGRATIONS.md](docs/EXTERNAL-INTEGRATIONS.md) | External setup |

## Quick start

```bash
uv sync --locked && cp .env.example .env            # fill in .env
docker compose -f infra/docker-compose.yml up -d --wait   # Floci (DynamoDB, SQS, S3) on 127.0.0.1:4566
sh infra/floci/init/bootstrap.sh                          # tables, job queue + DLQ, bucket; idempotent
docker build -t ai-code-change-agent/execution-python:0.2.0 docker/execution
docker compose up -d --wait api && ngrok http 8000  # point the Jira webhook here
uv run ai-code-change repositories register --from-provider <owner>/<repo> --project <KEY> --enable
uv run ai-code-change worker
```

## Local AWS emulator (Floci)

[Floci](https://github.com/floci-io/floci) 2.5.0, pinned by digest, is the **local AWS endpoint** of the API
and the worker in development and test ([ADR-014](docs/decisions/ADR-014-floci-local-aws-emulator.md),
[ADR-015](docs/decisions/ADR-015-dynamodb-sqs-s3-persistence.md)). It runs as its own Compose project
`acca-floci` on `127.0.0.1:4566` with the dummy credentials `test`/`test`. Host processes use
`ACCA_AWS_ENDPOINT_URL=http://127.0.0.1:4566`; the API container joins the `acca-floci` network and uses
`http://acca-floci:4566`. Settings refuse any non-local endpoint in development and test and any endpoint
override in staging and production.

```bash
docker compose -f infra/docker-compose.yml up -d --wait   # own project "acca-floci"
sh infra/floci/init/bootstrap.sh                          # idempotent; loopback + test/test only
uv run pytest -m floci -rs                                # skips with a reason when Floci is down
```

The web console opens on demand at `http://127.0.0.1:4566/_floci/ui` (a pinned `floci-ui` sidecar on
`127.0.0.1:4500`); see [docs/DOCKER-DEBUGGING.md](docs/DOCKER-DEBUGGING.md).

When a schema change alters a DynamoDB index (for example a GSI projection), the bootstrap refuses the
existing local table (exit 5) because DynamoDB cannot change a projection in place. Floci holds local
dev data only; recreate the tables from `infra/floci/schema` (their items are lost):

```bash
ACCA_DYNAMODB_RECREATE_ON_DRIFT=1 sh infra/floci/init/60-dynamodb.sh
```

Stop and clean up (Floci-spawned containers and volumes are labelled `floci=true` and are not part of the Compose project). `down -v` deletes all local application data (tables, queue, bucket):

```bash
docker rm -f $(docker ps -aq --filter label=floci=true)   # e.g. the ECR registry sidecar
docker compose -f infra/docker-compose.yml down -v        # Floci container, network and data volume
docker volume ls --filter label=floci=true                # remove leftovers with docker volume rm
```

- Never export `AWS_ENDPOINT_URL` in the shell that runs the worker: the DynamoDB, SQS, S3 and Floci-mode Bedrock clients ignore it, but the real-Bedrock client (`LLM_PROVIDER=bedrock` without `ACCA_BEDROCK_ENDPOINT_URL`) uses the standard AWS chain and would follow it.
- Never run `docker compose ... --remove-orphans` on project `ai-code-change-agent`: an `acca-postgres` container left by an older checkout counts as an orphan and would be removed.
