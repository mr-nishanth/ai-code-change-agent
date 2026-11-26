# Project initialization report

> **Historical record.** Repository names, ids and commands below are from that run. Onboarding is now generic: `register --from-provider <owner>/<repo> --project <KEY> --enable` (see AUTOMATED-PIPELINE.md); no code or document is specific to a repository.

Date: 2026-11-25.

- **Project:** `ai-code-change-agent` at `/Users/nishanth/Documents/tekion/ai-code-change-agent`. Python package `ai_code_change_agent`; product **AI Code Change Agent**.
- **Source (read only):** `/Users/nishanth/Documents/tekion/ai-migration-agent-python`.
- **Architecture:** 1 LangGraph workflow + 1 Deep Agent + 1 independent Python validation engine. The nine owner decisions are recorded in [ARCHITECTURE-DECISIONS.md](ARCHITECTURE-DECISIONS.md).

## Structure

```text
ai-code-change-agent/
├── src/ai_code_change_agent/
│   ├── main.py, composition.py
│   ├── api/                 routes (health, POST /webhooks/jira), middleware
│   ├── application/         webhooks, jobs (queue, worker), changes, repositories, jira,
│   │                        git_providers (ports), delivery, artifacts, workspaces, errors
│   ├── domain/              webhooks, changes (states, approvals), repositories (registry, policy)
│   ├── agents/coding/       the Deep Agent: agent, prompts, schemas, configuration
│   ├── orchestration/       langgraph (graph, state, nodes, routing, checkpointer, runner),
│   │                        steps.py, intake.py
│   ├── validation/          engine, rules, security_scanner, git_inspector, test_runner, decision
│   ├── execution/           docker (runtime, sandbox, process), fs, baseline, artifacts
│   ├── integrations/        github (auth, baseline, publisher, references, repositories), jira
│   ├── providers/llm/       chat model factory (Anthropic)
│   ├── persistence/         models, repositories, migrations (fresh 0001), session
│   ├── core/                config, logging, errors
│   └── cli/                 repositories, changes, worker
├── docker/api/Dockerfile, docker/execution/{Dockerfile,requirements.txt}
├── tests/                   unit/, integration/, support/, fixtures/
├── docs/                    13 documents
├── compose.yaml, alembic.ini, pyproject.toml, uv.lock, .python-version
├── .env (0600, not for commit), .env.example, .gitignore, .dockerignore
└── README.md, AGENTS.md, CLAUDE.md
```

146 source modules and 54 test modules. File-by-file traceability is in [MIGRATION-MAP.md](MIGRATION-MAP.md).

## What was reused, adapted, rebuilt and excluded

- **Reused (copied, imports rewritten):**
  - Jira: webhook verification and intake, REST client, ADF, mapper.
  - GitHub App: authentication, credentials, baseline reader, publisher, reference parser, registration client.
  - Repository registry, profiles and command policy.
  - Artifact store and service, workspace runtime, file safety, process runner.
- **Adapted:**
  - Webhook repository: the intake job is written in the same transaction as the receipt.
  - Webhook route moved to `/webhooks/jira`.
  - CLI gained `worker` and `changes` commands.
  - Artifact kinds updated.
  - Workspace runtime gained `tree_path`.
  - Execution image renamed.
  - Migrated tests updated to the new names.
- **Rebuilt:**
  - Settings.
  - Composition root.
  - Change domain: states, attempts, hash-bound approvals, change store.
  - Job queue and worker.
  - Deep Agent.
  - Docker sandbox backend.
  - LangGraph workflow and steps.
  - Validation engine.
  - Delivery manifest and PR text.
  - Jira story extraction and reporter.
  - LLM factory.
  - Fresh Alembic history.
  - Compose and Dockerfiles.
  - All documents.
- **Excluded:** see [MIGRATION-MAP.md](MIGRATION-MAP.md#excluded): the old agents and workflow, the LLM gateway and adapters, local bare delivery, the old migrations, the task-category taxonomy, and historical docs.

## Environment configuration

- **`.env`** was created by an allow-listed script (mode 0600). No value was displayed.
  - 21 keys were migrated: Jira webhook and REST, GitHub App ID, installation ID and key path, Anthropic key and model, log settings.
  - New local values were set for the database, workspace and artifact roots, the execution image, and the checkpoint schema.
  - Delivery, PR creation and Jira reporting are **false**.
  - Not migrated:
    - the old `DATABASE_URL`;
    - Redis, outbox and `MIGRATION_*` keys;
    - `GRAPHIFY_*` and `DOCKER_EXECUTION_*`;
    - `JIRA_FIELD_*` (DMS-era);
    - `ADMIN_API_KEY` and `APP_RUNTIME`.
- **`.env.example`** contains every variable, grouped by category, with placeholders only.
- **Settings check:** the real `.env` loads as valid, and the GitHub private key file is valid (RSA, owner-only). No missing required variables.

## Integration status

| Area | Status |
|---|---|
| Jira | Same site (`mr-nishanth.atlassian.net`), project `KAN`, Story type, webhook secret. The Jira webhook URL must be changed to `/webhooks/jira` when this project is deployed. No real Jira call was made. [SETUP-JIRA-WEBHOOK.md](SETUP-JIRA-WEBHOOK.md) |
| GitHub App | Same App (5884606) and installation (184929181). No App setting changed. No real GitHub call was made from this project. [SETUP-GITHUB-APP.md](SETUP-GITHUB-APP.md) |
| Anthropic | Key and model configured; validity not checked (no model call). |
| Docker | Execution image `ai-code-change-agent/execution-python:0.1.0` built (Python 3.12.16, coreutils `timeout`, pytest 9.2.0, ruff 0.17.6, mypy). API image built. |
| Database | New container `acca-postgres` (127.0.0.1:5435, volume `acca_pgdata`, database `ai_code_change_agent`, test database `ai_code_change_agent_test`). The old database container (`aima-python-postgres`, 5434) was not touched. |
| Dependencies | `uv.lock` with 91 packages. Key versions: deepagents 0.7.41, langgraph 1.2.24, langgraph-checkpoint-postgres 3.2.1, langchain-anthropic 1.7.12, langchain-core 1.8.4, fastapi 0.165.0, sqlalchemy 2.1.9, psycopg 3.3.9, pydantic 2.14.4. |

## Verification (exact results)

| Check | Result | Evidence class |
|---|---|---|
| `uv sync --locked` | OK (91 packages) | local |
| Imports (`composition`, `main`, `cli.main`) | OK | unit |
| `uv run ruff check .` / `ruff format --check .` | All checks passed / 242 files formatted | static |
| `uv run mypy` (strict) | no issues in 226 source files | static |
| `uv run pytest` (full) | **935 passed, 0 skipped, 0 failed**; coverage **95.70%** (threshold 90%) | unit + integration + local infra |
| `pytest -m docker` | 17 passed: workspace isolation, sandbox isolation (no network, read-only root, no socket, no credentials in env, no capabilities, timeout kill, output cap, disk limit), read-only planning mount, **the Deep Agent planning and implementing inside the real sandbox** with a scripted model | local infrastructure |
| `pytest -m integration` | 42 passed: registry, webhook to job in one transaction, job queue (SKIP LOCKED, leases, crash reclaim, retry/dead), database triggers, Postgres checkpointer resume after restart, CLI approval enqueuing a resume | local infrastructure |
| Alembic | Upgrade, downgrade to base and upgrade again on the empty new database; `alembic current` = `0001 (head)`; `alembic check`: no new upgrade operations | local infrastructure |
| FastAPI on the host | `/health/live` 200; `/health/ready` 200 (database true); unsigned `POST /webhooks/jira` 401 | local infrastructure |
| Compose API | Image built; container healthy; non-root (uid 10001), read-only root, capabilities dropped, no host mounts; receives only the webhook variables; ready 200; unsigned webhook 401; stopped afterwards | local infrastructure |
| Worker | `uv run ai-code-change worker --once` with the real configuration: exit 0 (empty queue). The checkpointer created `langgraph.{checkpoints,checkpoint_writes,checkpoint_blobs,checkpoint_migrations}` | local infrastructure |
| Deep Agent | Built with the real `ChatAnthropic` class and `DockerSandbox` backend (`CompiledStateGraph`); no model call | unit |
| LangGraph | The graph compiles; full Story to draft PR path on test doubles with both gates, repair, rejection, blocked, delivery disabled | integration with test doubles |
| Markdown links | 14 files, 24 relative links, 0 missing | static |

**Not verified:**
- a real Jira webhook delivery or REST call;
- any real GitHub call from this project;
- a real Anthropic model call;
- the full workflow against real services.

## Old project

The working tree is unchanged.

- `git status` in `ai-migration-agent-python` is clean against its commit `4aa7a71`, which was not made by this task. It has no remotes.
- Files under the old project's `.swarm/` and `.claude-flow/` were modified by session tooling (agent hook plugins), not by this task, and are ignored by git.
- Its database container was not used.
- The old `.env` was only read, by the allow-listed migration script.

## Outstanding items

1. Real external verification, when you authorise it:
   - one Jira webhook delivery to `/webhooks/jira`;
   - one read-only GitHub baseline read from this project;
   - one Anthropic model call;
   - Jira **Add Comments** permission before enabling reporting.
2. Register repositories in the new registry, using `--from-provider` plus a profile, project mapping and `enable`. The new database is empty.
3. The workflow has a single execution image (python toolchain). Other toolchains are BLOCKED until an image exists.
4. Operator identity is unverified until SSO.
5. Production worker deployment (see ARCHITECTURE.md): a dedicated host or a remote sandbox service.
6. The repository is not under version control yet. Nothing was committed, by instruction.
