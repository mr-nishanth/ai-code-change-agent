# Migration inventory: ai-migration-agent-python -> ai-code-change-agent

> **Historical record.** Repository names, ids and commands below are from that run. Onboarding is now generic: `register --from-provider <owner>/<repo> --project <KEY> --enable` (see AUTOMATED-PIPELINE.md); no code or document is specific to a repository.

Status: **complete (Stages A-H), 2026-11-25.** The audit below drove the migration; the file-level result is in [MIGRATION-MAP.md](MIGRATION-MAP.md). The source repository was only read; nothing in it was modified.

The source of truth is section 2 of the extraction prompt (1 LangGraph workflow + 1 Deep Agent + 1 independent Python validation engine). No separate "updated architecture document" exists in either repository or under `~/Documents/tekion`. The prompt's section 2 is treated as that document.

## Evidence levels used below

| Code | Meaning |
|---|---|
| U | Unit tests (in-memory fakes) |
| I | Integration with test doubles (fake HTTP transports, fake GitHub backed by real `git`) |
| L | Local infrastructure (real PostgreSQL in Docker, real Docker daemon, real `git`) |
| X | Real external service, with the exact scope stated |
| - | No evidence |

Source suite at audit time: 1751 passed, 4 skipped (opt-in Ollama), coverage 96.52%. One Docker cancellation test is known to be flaky (failed once, not reproduced).

## Inventory

| # | Component (source path under `src/ai_code_change_agent/`) | Responsibility | Evidence | Fit with new architecture | Class | Target in new project |
|---|---|---|---|---|---|---|
| 1 | `domain/webhooks`, `application/webhooks`, `api/routes` (Jira webhook) | HMAC (`X-Hub-Signature`) verification, body size cap, project/issue-type/event policy, durable receipt, duplicate delivery suppression | U, L (Postgres); **real Jira webhook never received** | Matches 2.1. The route path changes to `POST /webhooks/jira`. Dispatch to a job instead of in-request work is new. | ADAPT | `integrations/jira/webhook.py`, `api/routes/jira_webhook.py`, `application/jobs` |
| 2 | `integrations/jira/{client,authentication,pagination,adf,mapper,adapter}.py`, `application/integrations/jira` | Read-only Jira REST: basic auth, retries, size caps, ADF to text, issue mapping, comments | U, I; **real Jira REST never called** | Matches 2.1 (read). Story extraction of acceptance criteria needs adding. Writes (2.7) do not exist. | ADAPT (read) + NEW (reporting) | `integrations/jira/{client,authentication,issues}.py` |
| 3 | `integrations/git_providers/github_app/credentials.py`, `auth.py` | Key-file checks, RS256 App JWT, installation tokens down-scoped per repository and scope (read/write), fresh token per operation, typed errors | U, I; **X: one draft PR and one read-only baseline on `automate_connector`** | Matches 2.6 | REUSE | `integrations/github/authentication.py` |
| 4 | `integrations/git_providers/github_app/reader.py` | Pin default branch to one commit; verified tree listing; tarball via fixed codeload host without bearer token; full integrity checks | U, I; **X: one live read (1-file repo), hash matched an independent clone** | Matches 2.2 (baseline) | REUSE | `integrations/github/baseline.py` |
| 5 | `integrations/git_providers/github_app/{publisher,gitobj}.py`, `application/delivery/pull_request.py` | Create-only blobs, tree, commit and ref through the Git Database API; draft PR; reconciliation of unknown outcomes; re-read verification | U, I; **X: one draft PR via a scratch script (not through the workflow)** | Matches 2.6. Deliberately has no push: commits are created through the API, which needs no Git credentials in any process. | REUSE | `integrations/github/{commits,branches,pull_requests}.py` (split by responsibility) |
| 6 | `integrations/git_providers/github_app/{reference,repository_client}.py` | Story reference parser (alias, owner/name, https URL); registration helper (`--from-provider`) | U, I; registration helper not run live | Matches 2.2 / 5.3 | REUSE | `integrations/github/repositories.py` |
| 7 | `domain/repositories`, `application/repositories`, `persistence/*registry*` | Registry: canonical id, status, aliases, immutable profile versions, project mappings, audit, resolver, command policy | U, L (Postgres) | Matches 2.2 / 5.3. The profile schema must also describe what the Deep Agent may run (section 4, decision C2). | ADAPT | `repositories/{registry,resolver,profiles}.py`, `execution/docker/policy.py` |
| 8 | `domain/changes`, `application/changes`, `persistence/*change*` | CodeChangeRequest and Attempt aggregate, 14-state machine, hash-bound PLAN and FINAL_DELIVERY approvals, one active attempt, outbox events, DB-enforced projections | U, L | The safety guarantees carry over. The **state list is built around Analysis/Planning/Implementation agents** and must be redesigned for one Deep Agent run. | REBUILD (states) / REUSE (approval hashing, duplicate guards) | `application/approvals`, `application/jobs`, `persistence/models` |
| 9 | `application/llm`, `integrations/llm/{anthropic,bedrock,ollama}` | Custom LLMGateway with structured output and repair loop over raw SDK clients | U; Ollama live opt-in | Deep Agents needs LangChain `BaseChatModel` instances, so these adapters cannot drive the agent. | REBUILD | `providers/llm/factory.py` (LangChain chat models) |
| 10 | `application/workflows/*`, `integrations/workflow/langgraph` | 05A LangGraph analysis and planning graph, prompts, schemas, plan approval pause/resume, Gate C implementation stage, rework | U, L | Built around the old multi-agent split; must not be carried forward. Reusable ideas: approval-pause pattern, failure classification. | REBUILD | `orchestration/langgraph/*` |
| 11 | `application/agents/implementation` | Plan-scoped patch applier (the model returns JSON edits; the platform applies them) | U, L | Replaced by the Deep Agent's own filesystem tools working inside the sandbox | EXCLUDE | - |
| 12 | `application/agents/validation` | Deterministic validation: rebuilds a fresh workspace from baseline + patch, scope/protected/secret checks, profile commands, report | U, L (Docker) | Matches 2.5. Its input changes from a JSON patch to the sandbox diff. | ADAPT | `validation/{engine,rules,git_inspector,security_scanner,test_runner,decision,schemas}.py` |
| 13 | `application/artifacts`, `integrations/artifacts` | Scoped, hash-verified, redacting artifact store (local filesystem) | U | Still needed (plan, diff, validation report, manifest) | REUSE | `execution/artifacts.py` |
| 14 | `application/workspaces`, `integrations/workspaces/docker` | Workspace creation from baseline, `dir_fd`/`O_NOFOLLOW` file ops, per-command `docker run` with `--network none`, read-only root, caps dropped, limits, cleanup | U, L (real Docker) | Hardening and file safety are reusable. The Deep Agent needs a **long-lived sandbox implementing `deepagents.backends.sandbox.BaseSandbox`** (`execute`, `upload_files`, `download_files`, `id`). | ADAPT | `execution/docker/{runtime,backend,policy}.py`, `execution/workspace.py` |
| 15 | `application/delivery/{manifest,service,records,errors,naming}.py` | Manifest bound to tree hashes, final approval, idempotent delivery records, outcome classification | U, L | Matches 2.6 coordination; the input wiring changes | ADAPT | `application/services/delivery.py` |
| 16 | `integrations/git_providers/local_bare.py`, `github.py` (read-only boundary) | Local bare-repo publisher (Gate D test target); disabled external boundary | U, L | Not part of the new architecture (GitHub only) | EXCLUDE | - (the fake GitHub covers tests) |
| 17 | `persistence/migrations/versions/0001-0005` | Alembic history, including the NestJS-era rename (0003) | L | History tied to the old schema and old names | REBUILD | One fresh `0001_initial` |
| 18 | `cli/main.py` | Registry administration (CLI-only, audited) | U, L | Still required (operator-controlled registration) | ADAPT | `cli/main.py` |
| 19 | `core/{config,logging,...}` | Pydantic settings (`APP_*`), structlog JSON logging, security helpers | U | The settings are rebuilt for the new variable names | ADAPT | `core/*` |
| 20 | `docker/workspace-python/Dockerfile` | Hash-pinned Python toolchain image | L | Base for the execution image; must also provide `python3` for BaseSandbox reads | ADAPT | `docker/execution/Dockerfile` |
| 21 | `compose.yaml` | Postgres 16 on 127.0.0.1:5434, project `ai-migration-agent-python` | L | The new project needs its own database | REBUILD | `compose.yaml` (new name, port, volume) |
| 22 | Domain "task category" catalogue, profile capabilities | Category to validation strictness | U | Still useful (bug fix, feature, migration...) | REUSE | `repositories/profiles.py` |
| 23 | Old docs (`docs/01-12*`, NestJS inventory, module reports) | History | - | Not carried forward | EXCLUDE | Referenced from `MIGRATION-MAP.md` only |
| 24 | `queue/`, `publishing/`, `observability/`, `orchestration/` (empty packages) | Placeholders | - | - | EXCLUDE | - |

## Source `.env` (key names only; values not displayed)

| Keys | Action |
|---|---|
| `APP_DATABASE_URL` | **Not migrated.** The new project gets its own database. |
| `APP_JIRA_WEBHOOK_SECRET`, `APP_JIRA_BASE_URL`, `APP_JIRA_EMAIL`, `APP_JIRA_API_TOKEN`, `APP_JIRA_ALLOWED_PROJECT_KEYS`, `APP_JIRA_ALLOWED_ISSUE_TYPES` | Migrate under the new names |
| `APP_GITHUB_APP_ID`, `APP_GITHUB_INSTALLATION_ID`, `APP_GITHUB_PRIVATE_KEY_PATH` | Migrate. The key **file** stays where it is and only the path is copied. |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` (unprefixed, NestJS era) | Migrate if Anthropic is the provider (decision C5). Whether the key is still valid is unverified. |
| `APP_DELIVERY_ENABLED`, `APP_GITHUB_PR_CREATE_ENABLED` | Re-created **false** in the new project (no real writes during extraction) |
| `REDIS_*`, `MIGRATION_*`, `OUTBOX_*`, `GRAPHIFY_*`, `DOCKER_EXECUTION_*`, `JIRA_FIELD_*`, `JIRA_REST_ENABLED`, `JIRA_CUSTOM_FIELDS_ENABLED`, `ADMIN_API_KEY`, `APP_RUNTIME` | **Not migrated** (obsolete NestJS-era or unused) |

The owner approved all nine architecture decisions (refined): [ARCHITECTURE-DECISIONS.md](ARCHITECTURE-DECISIONS.md).

Outcomes that differ from this table:

- **Row 22 (task categories):** classified REUSE, but excluded; nothing in the new flow uses it.
- **Row 9 (LLM layer):** rebuilt as a LangChain factory (Anthropic only).
