# AGENTS.md

Rules for any coding agent working in this repository. Product: **AI Code Change Agent**: Jira Story to draft pull request for any authorised repository, language or task category.

## Architecture (fixed)
- **One parent LangGraph workflow** (`orchestration/langgraph`), **one Deep Agent** (`agents/coding`, Deep Agents SDK `create_deep_agent`) and **one independent Python validation engine** (`validation/`). Do not add agent roles (no separate analyzer or planner agents) or agent services.
- The Deep Agent runs in two phases:
  - **planning:** read-only sandbox, structured plan;
  - **implementation:** writable sandbox.

  Two gates bind to exact hashes (plan, then manifest). By default (`WORKFLOW_APPROVAL_MODE=automatic`) deterministic policy decides them (actor `policy:automatic`); `manual` restores operator approval. Changing an artifact invalidates its approval. See docs/AUTOMATED-PIPELINE.md.
- The validation engine decides PR eligibility. The agent's report is never authoritative.
- Multi-repository Stories: a deterministic coordinator (`application/multirepo`) runs one isolated child attempt per repository of the same workflow; no extra agents (ADR-013).
- Cost guards are mandatory for every model: model-call cap, phase USD budget, bounded structured-output retries. Do not remove them.
- One deployable application, two processes:
  - **API** (FastAPI, webhook intake only);
  - **worker** (host process via `uv run ai-code-change worker`), the trusted control plane.

  The worker also runs the job dispatcher (outbox relay and lost-message sweep) as a loop, not a separate service.

  Persistence is AWS-native (ADR-015): DynamoDB is the only database (application table + checkpoint table), job state lives in DynamoDB and SQS (standard queue + DLQ) only delivers job ids, artifacts are write-once S3 objects, LangGraph checkpoints use `DynamoDBSaver` with S3 offload. The API never calls SQS or S3. No Redis, no PostgreSQL. Locally all of it runs on Floci (ADR-014).

## Security invariants
- The Jira payload never grants access. Repository access requires both:
  - membership in the GitHub App installation (fresh token per operation);
  - the registry: ACTIVE repository, project mapping and published profile. Never hard-code a repository, project or owner in code; onboarding is `ai-code-change repositories register --from-provider <owner>/<repo> --project <KEY> --enable`.

  There is no static allowlist and no automatic registration.
- **Sandbox hardening is fixed in code** (`execution/docker/sandbox.py`):
  - no network, non-root user, read-only root filesystem, all capabilities dropped, `no-new-privileges`, default seccomp;
  - resource limits; no Docker socket, host paths or credentials.

  Dependencies come only from a trusted, hash-locked wheel bundle prepared by the worker and mounted read-only (ADR-010); anything else fails closed. Never enable networking in the sandbox. The agent's only custom tools are the harness-side `search_docs` and `fetch_reference` (exact trusted hosts from `config/reference_sources.yaml`, any path on them, no implied subdomains; run in the worker; ADR-011, docs/PYTHON-EXECUTION-AND-RESEARCH.md).
- **Credentials:**
  - The worker holds GitHub, Jira and LLM credentials. The API holds only the webhook secret.
  - The agent and its sandboxes hold none.
  - Never log or persist tokens, keys or `.env` values.
- **GitHub writes:** only through `integrations/github/publisher.py`:
  - create-only refs and draft PRs;
  - never merge, force-push or write the default branch;
  - off unless `CODE_CHANGE_DELIVERY_ENABLED` and `GITHUB_PR_CREATE_ENABLED`.
- **Jira writes:** only `integrations/jira/reporting.py` (one outcome comment), off unless `JIRA_REPORTING_ENABLED`.
- **SDK confinement:** `deepagents`, `langgraph`, `langchain_anthropic`, `jwt` and `cryptography` stay in their packages. `tests/unit/test_architecture.py` guards this.

## Engineering
- Python 3.12 and `uv` only (`uv sync`, `uv add`, `uv run`). Commit `uv.lock`.
- Pydantic v2, boto3 (DynamoDB, SQS and S3 clients only from `integrations/aws/clients.py`), `langgraph-checkpoint-aws==1.3.0` (exact pin), structlog. There are no migrations: table schemas live in `infra/floci/schema/` and every invariant is a condition expression or transaction.
- Configuration uses unprefixed names, except the AWS settings, which use `ACCA_` so the shell's `AWS_*` variables never configure the application (see `docs/CONFIGURATION.md`). `.env` is never committed. `.env.example` has placeholders only.
- **AWS endpoints:** development and test always use the local Floci endpoint (`http://127.0.0.1:4566` from the host, `http://acca-floci:4566` from containers) with the dummy credentials `test`/`test`; never weaken the endpoint guard in `core/config.py`. Real AWS calls need explicit owner authorisation, like real model calls.
- **Local services:** `docker compose -f infra/docker-compose.yml up -d --wait`, then `sh infra/floci/init/bootstrap.sh`. Never pass `--remove-orphans` to `docker compose` on project `ai-code-change-agent` (it would remove a running `acca-postgres` left by an older checkout). Debugging: `docs/DOCKER-DEBUGGING.md`.
- **Quality gate before reporting done:**
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy` (strict)
  - `uv run pytest` (coverage >= 90%; with Floci up and bootstrapped, so the DynamoDB, SQS and S3 tests run instead of skipping)

  Report exact results; never claim an unexecuted check.
- Tests first for new behaviour. Real external calls (Jira writes, GitHub writes, model calls, real AWS) need explicit owner authorisation. AWS tests use explicit local endpoints and dummy credentials only (`tests/support/floci.py` or `AwsClients` with test Settings).
