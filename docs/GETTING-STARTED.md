# Getting started: setup and run

## 1. Prerequisites

- **Local tools:** Python 3.12, [uv](https://docs.astral.sh/uv/), Docker Desktop, ngrok (or another public tunnel) and Node 22 (only for the Studio UI). Optional debugging tools: [DOCKER-DEBUGGING.md](DOCKER-DEBUGGING.md).
- **Accounts:** a Jira Cloud site, a GitHub App installed on the repositories you want to change ([SETUP-GITHUB-APP.md](SETUP-GITHUB-APP.md)), and an Anthropic API key (local) or AWS Bedrock access (staging and production).

## 2. Install and configure

```bash
uv sync --locked
cp .env.example .env && chmod 600 .env      # fill in values; never commit .env
```

Minimum `.env` values:
- **AWS (local Floci):** `ACCA_AWS_ENDPOINT_URL=http://127.0.0.1:4566` (required in development and test) and the `ACCA_*` table, queue and bucket names (the `.env.example` defaults match the bootstrap).
- **Jira:** `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_WEBHOOK_SECRET`, `JIRA_PROJECT_KEYS`.
- **GitHub App:** `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`.
- **Model:** `LLM_PROVIDER=anthropic`, `LLM_MODEL=claude-haiku-5-5`, `ANTHROPIC_API_KEY`. For production use `LLM_PROVIDER=bedrock` with `BEDROCK_REGION` and AWS credentials.
- **Workspaces:** `WORKSPACE_ROOT`, `ARTIFACT_ROOT` (absolute paths) and `WORKSPACE_IMAGE`.
- **Delivery:** `CODE_CHANGE_DELIVERY_ENABLED=true`, `GITHUB_PR_CREATE_ENABLED=true`, `JIRA_REPORTING_ENABLED=true`.

All variables are described in [CONFIGURATION.md](CONFIGURATION.md).

## 3. Local AWS (Floci), images and API

```bash
docker compose -f infra/docker-compose.yml up -d --wait   # Floci: DynamoDB, SQS, S3 on 127.0.0.1:4566
sh infra/floci/init/bootstrap.sh                     # tables, job queue + DLQ, bucket (idempotent)
docker build -t ai-code-change-agent/execution-python:0.2.0 docker/execution
mkdir -p -m 700 var/workspaces var/artifacts
docker compose up -d --wait api                      # webhook intake on 127.0.0.1:8000 (joins the acca-floci network)
```

DynamoDB starts empty: there is nothing to migrate ([ADR-015](decisions/ADR-015-dynamodb-sqs-s3-persistence.md)). Floci data lives in its own volume until `docker compose -f infra/docker-compose.yml down -v`. Do not add `--remove-orphans` to `docker compose` for this project while an older `acca-postgres` container must keep running.

Only Python repositories are supported; the Python image is the only execution image.

## 4. Jira webhook

```bash
ngrok http 8000      # gives https://<id>.ngrok-free.dev
```

In Jira, go to Settings → System → WebHooks:
- **URL:** `https://<id>.ngrok-free.dev/webhooks/jira`;
- **Event:** issue created;
- **Secret:** the value of `JIRA_WEBHOOK_SECRET`.

Details are in [SETUP-JIRA-WEBHOOK.md](SETUP-JIRA-WEBHOOK.md).

## 5. Onboard repositories (one command each)

```bash
uv run ai-code-change repositories register --from-provider <owner>/<repo> --project <JIRA_KEY> --enable
#   --test-command "..." via init-profile to override
uv run ai-code-change repositories list
```

## 6. Run the worker

```bash
caffeinate -i uv run ai-code-change worker     # macOS: keep the machine awake; Ctrl+C stops it
```

The worker prints a cost report per attempt and a session summary when it stops. Each job it claims is logged as `job_claimed` with `job_id`, `kind`, `attempt` and `max_attempts` (never the payload).

## 7. Create a Story

Single repository:

```text
Add a /health endpoint with a test.
Repository: <owner>/<repo>
```

Several repositories ([MULTI-REPOSITORY.md](MULTI-REPOSITORY.md)):

```text
Repositories:
- Repository: <owner>/backend
  Role: backend
  Responsibility: ...
- Repository: <owner>/client
  Role: client
  Depends on: backend
```

Result: the Story gets a Jira comment and each repository gets a draft PR to review.

## 8. Watch it (optional): Deep Agent Studio

```bash
GRPC_VERBOSITY=ERROR GRPC_ENABLE_FORK_SUPPORT=false uv run langgraph dev --no-browser --no-reload --port 2024
cd deep-agent-ui && npm install && npm run dev     # http://127.0.0.1:5173
```

Go to History → Jira pipeline, click an attempt, then **Watch live**. See [DEEP-AGENT-STUDIO.md](DEEP-AGENT-STUDIO.md).

## 9. Operate

```bash
uv run ai-code-change costs --issue <KEY>               # LLM cost per stage, attempt, repository
uv run ai-code-change changes show <attempt-id>
uv run ai-code-change changes reject <attempt-id> --gate PLAN --sha256 <hash> --reason "why"  # manual mode
uv run ai-code-change changes retry-child <child-id>    # re-run one failed repository of a Story
```

## 10. Quality checks

```bash
uv run pytest                      # unit + integration (coverage >= 90%; Floci up and bootstrapped)
uv run pytest -m floci -rs         # DynamoDB, SQS, S3 and checkpoint tests only
uv run pytest -m docker            # Docker sandbox tests (run when no worker sandbox is active)
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
