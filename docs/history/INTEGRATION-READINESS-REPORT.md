# Integration readiness report (S2040)

> **Historical record.** Repository names, ids and commands below are from that run. Onboarding is now generic: `register --from-provider <owner>/<repo> --project <KEY> --enable` (see AUTOMATED-PIPELINE.md); no code or document is specific to a repository.

> **Historical report.** Since 2026-11-26 the only LLM providers are `anthropic` (local development and tests) and `bedrock` (staging and production). The Ollama and NVIDIA providers described below were removed. See [CONFIGURATION.md](CONFIGURATION.md).

Date: 2026-11-25. Scope: read-only verification of real external dependencies before onboarding the first repository. **No external writes were performed**: no Jira issue, comment, status or webhook change; no GitHub branch, commit, PR, App or installation change. Delivery and Jira reporting stayed disabled throughout. No repository was registered.

## 1. Results

| Integration | Check | Result | Notes |
|---|---|---|---|
| Jira | Authentication | PASSED | `GET /rest/api/3/myself` 200 through the project's `JiraHttpClient` (Basic e-mail + API token); account active |
| Jira | Project KAN access | PASSED | `GET /rest/api/3/project/KAN`: "Jira Ticket automations" (id 10001, team-managed). Issue types include Story. BROWSE_PROJECTS granted |
| Jira | Story retrieval | PASSED | JQL `project = KAN AND issuetype = Story` returned KAN-2..KAN-6; `JiraCloudIssueReader.get_issue("KAN-6")` and `get_comments` mapped cleanly (0 comments) |
| Jira | Webhook configuration | BLOCKED | Webhook "AI Migration Agent TriggerNew Webhook Listener": enabled, `jira:issue_created`, JQL `project = KAN AND issuetype = Story`, body included, URL `https://tying-spelling-customary.ngrok-free.dev/webhooks/jira`. The URL matches the running ngrok tunnel and this app's path, **but nothing listens on 127.0.0.1:8000** (API not started), so a delivery would fail now. Not changed |
| GitHub | App authentication | PASSED | RS256 App JWT; `GET /app` 200: `ai-code-change-agent`, owner mr-nishanth, permissions contents:write, metadata:read, pull_requests:write; no webhook events |
| GitHub | Installation authentication | PASSED | `GET /app/installations/184929181` 200 (User mr-nishanth, selected repositories, not suspended). Installation-token exchange 201, ~3600 s lifetime, down-scoped grants verified by `GitHubAppAuth` |
| GitHub | Repository listing | PASSED | `GET /installation/repositories`: 4 repositories (section 5) |
| GitHub | Repository metadata | PASSED | For all 4: `GitHubAppRepositoryClient.describe_repository` (the `--from-provider` path) returned id + default branch `master`; per-repository read token; languages, branch head and root contents read |
| Anthropic | API authentication | BLOCKED | 401 `authentication_error` on `GET /v1/models` and through `ChatAnthropic`. Root cause: `ANTHROPIC_API_KEY` in `.env` is a 10-character placeholder, not a key. Error text contains no key material |
| Anthropic | Model invocation | BLOCKED | `LLM_MODEL=REPLACE_WITH_SUPPORTED_MODEL` (placeholder). Needs a real key and model id from the owner |
| Ollama (local, new) | Model invocation | PASSED | `LLM_PROVIDER=ollama`, `llama3.1:latest` on `http://127.0.0.1:11434` via `build_chat_model`: exact `AI_CODE_CHANGE_AGENT_CONNECTION_OK` in 4.4 s. Unknown model raises `ollama.ResponseError` 404 |
| Bedrock (new) | Model invocation | NOT TESTED | Factory implemented and unit-tested; no AWS region/credentials configured and no authorisation for a live AWS call |
| Docker | Execution image | PASSED | `ai-code-change-agent/execution-python:0.1.0` (c3ce031dc5be, 362 MB) present; matches `WORKSPACE_IMAGE` |
| PostgreSQL | Database health | PASSED | `acca-postgres` healthy on 127.0.0.1:5435; `pg_isready` OK; Alembic at `0001 (head)` |

All live checks ran through the project's own adapters where one exists. A probe compared every captured output and log line against the configured secrets: **no leak detected**.

## 2. Failures and blockers

| Item | Root cause | Component | Correction | Code change? | Owner action? |
|---|---|---|---|---|---|
| Anthropic auth | Placeholder `ANTHROPIC_API_KEY` (10 chars) in `.env` | configuration | Put a valid Anthropic API key in `.env` | No | **Yes** |
| Anthropic model | `LLM_MODEL` placeholder | configuration | Set a supported model id (or `llama3.1:latest` while `LLM_PROVIDER=ollama`) | No | **Yes** |
| Webhook delivery | API not running on port 8000; ngrok free hostname is ephemeral | runtime | `docker compose build api && docker compose up -d --wait api`, keep the tunnel up, then create one test Story (authorised step) | No | **Yes** |
| Bedrock | Not configured | configuration | `LLM_PROVIDER=bedrock`, `LLM_MODEL=<model or inference-profile id>`, `BEDROCK_REGION`, worker IAM role/profile with `bedrock:InvokeModel` (+ `bedrock:Converse`) on that model only | No | **Yes** |

## 3. Change made in this task: LLM provider selection (owner request)

The owner asked for a local Ollama model for tests and AWS Bedrock for beta and production. Implemented inside the existing provider factory only (`providers/llm/factory.py`); workflow, agent and validation engine untouched.

| Provider | Use | Settings | Guard |
|---|---|---|---|
| `anthropic` | any environment (default) | `LLM_MODEL`, `ANTHROPIC_API_KEY` | unchanged |
| `bedrock` | beta (staging) and production | `LLM_MODEL`, `BEDROCK_REGION`, optional `BEDROCK_CREDENTIALS_PROFILE` | AWS credentials come only from the worker's standard AWS chain, never from Settings; region/profile format validated |
| `ollama` | local development and tests | `LLM_MODEL`, `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`) | loopback `http://` origin only; **refused when `APP_ENV` is staging or production** |

- Dependencies added with `uv add`: `langchain-ollama` (1.2.0) and `langchain-aws` (1.10.0, brings boto3/botocore). `uv.lock` updated.
- `tests/unit/test_architecture.py` confines `langchain_ollama`, `langchain_aws`, `boto3` and `botocore` to the factory.
- Tests: `tests/unit/test_llm_provider.py` (new).
- Docs: `.env.example`, `docs/CONFIGURATION.md`.
- Note: `llama3.1` (8B) is fine for connectivity checks but weak at the Deep Agent's tool calling. For a local end-to-end trial, `qwen3-coder:latest` (30B, already pulled) is the stronger local candidate. Real coding jobs should use Anthropic or Bedrock.

## 4. Repository baseline

| Item | State |
|---|---|
| Git | **Not initialised** (no `.git`). Plan: `git init -b main`, confirm `git status --ignored` lists `.env`, `*.pem`, `var/`, `.venv/`, `.coverage` as ignored, then a first commit only after owner approval. No commit or push was made |
| `.gitignore` / `.dockerignore` | Both exclude `.env`, `.env.*` (except `.env.example`), `*.pem`, `var/` |
| GitHub private key | Outside the project directory; mode 0600; owned by the worker user; valid RSA PEM |
| `.env` vs Settings | All `.env` keys are Settings fields. `.env.example` omits a few tuning fields that have safe defaults (e.g. `JIRA_*_TIMEOUT_SECONDS`, `ARTIFACT_MAX_*`); documented in CONFIGURATION.md |
| Compose | API container receives only `DATABASE_URL` and webhook settings (no GitHub, Jira REST, LLM or AWS credentials); read-only root fs, `cap_drop: ALL`, `no-new-privileges`, bound to 127.0.0.1 |
| Delivery / reporting flags | `CODE_CHANGE_DELIVERY_ENABLED=false`, `GITHUB_PR_CREATE_ENABLED=false`, `JIRA_REPORTING_ENABLED=false` |
| Server port | `APP_PORT` unset, so the default 8000 applies (compose maps 127.0.0.1:8000); currently not running |

## 5. First repository candidates

All four repositories in the installation are public, default branch `master` (unprotected), last pushed 2026-11-24/25. **Each contains only `README.md`**: no source code, no dependency manager, tests, linters, type checker or build commands. GitHub reports no language.

| Repository | Id | Head | Content | Python image fit |
|---|---|---|---|---|
| mr-nishanth/automate_connector | 1468634307 | 7708e5daaed0 | README (DMS connector description; architecture "to be documented") | Usable only for documentation-only changes; no test/lint commands exist to validate |
| mr-nishanth/autoSoft_connector | 1468634927 | 1f6865517a3f | README only | same |
| mr-nishanth/cdk_connector | 1468647812 | ede13b4bc5a4 | README only | same |
| mr-nishanth/reynolds_connector | 1468648410 | 45c612249a89 | README only | same |

Required execution environment for all of them: the existing Python image, provided the profile declares no install, test or lint commands needing network or packages. A meaningful first trial requires either (a) pushing a small Python package (e.g. `pyproject.toml`, `src/`, `tests/` with pytest) to the chosen repository first, or (b) accepting a documentation-only Story.

**Outstanding:** `automate_connector` draft PR #1 "[SMOKE-1] AI Code Change Agent change" (branch `aica/smoke-1-a1-d6c44a81e4d3`, opened 2026-11-25) is still open. Not modified.

### Onboarding commands (do not run until the owner selects and approves a repository)

Example for `automate_connector`; substitute the chosen repository. Profile content depends on the repository's real toolchain; run `uv run ai-code-change repositories toolchains` for the allowed commands, then validate offline.

```bash
# 1. Register (read-only identity lookup; stored DISABLED, no profile, no mapping)
uv run ai-code-change repositories register --key automate-connector \
    --display-name "Automate Connector" \
    --from-provider mr-nishanth/automate_connector --access-ref app-installation:184929181
# 2. Execution profile: validate offline, store as a draft, publish (immutable) and activate
uv run ai-code-change repositories validate-profile profiles/automate-connector.json
uv run ai-code-change repositories create-version automate-connector profiles/automate-connector.json
uv run ai-code-change repositories publish automate-connector 1
# 3. Jira project mapping
uv run ai-code-change repositories map-project KAN automate-connector
# 4. Activation, then a read-only resolver dry run
uv run ai-code-change repositories enable automate-connector
uv run ai-code-change repositories resolve --project KAN --reference automate-connector
```

## 6. Security and operational review

| Control | Status | Evidence |
|---|---|---|
| API receives only required credentials | OK | `compose.yaml` api environment: DB URL + webhook settings only |
| Worker credentials not in API container | OK | as above; worker is a host process |
| GitHub private key protected | OK | 0600, outside project, symlink/owner/size checked by `credentials.load_private_key` |
| No Docker socket / host paths in sandbox; no network | OK | `execution/docker/sandbox.py`: `--network none`, non-root, read-only rootfs, caps dropped, `no-new-privileges`, default seccomp, pids/memory/CPU/tmpfs limits |
| Planning read-only / implementation isolated | OK (by design and tests) | two-phase sandbox per AGENTS.md; per-attempt 0700 work dirs |
| Agent cannot publish to GitHub or modify Jira | OK | agent imports no integrations/config/httpx (architecture test); writes only via `publisher.py` / `reporting.py`, both disabled |
| Validation independent of agent claims | OK | `validation/` imports no model/agent SDK (architecture test) |
| Approval identity | Documented limitation | CLI actor is the OS user, unauthenticated until SSO (SECURITY.md) |
| Duplicate webhook delivery | OK | unique `delivery_id` and unique `jira_issue_id`; duplicate returns 200 `duplicate` |
| Raw-body HMAC | OK | `X-Hub-Signature: sha256=`, constant-time compare over the capped raw stream |
| Secret redaction | OK | structlog `redact_sensitive`; live probe found no secret in output or logs |
| New: LLM providers | OK | Ollama loopback-only and refused in staging/production; Bedrock uses the AWS chain on the worker only; SDKs confined to the factory |

Observations (no change made):
- The Jira account also holds CREATE_ISSUES, EDIT_ISSUES and ADD_COMMENTS in KAN. The code only uses GET (plus one optional comment), but a dedicated least-privilege account would reduce exposure.
- The App installation grants contents:write and pull_requests:write on all four repositories; the code down-scopes each token to one repository and the minimum permissions. Enabling branch protection on `master` adds defence in depth.
- The Jira webhook name still says "AI Migration Agent"; a rename is cosmetic and optional.

## 7. Test results

- Baseline before changes: ruff, format, strict mypy clean; 935 passed, coverage 95.70%.
- After the provider change: full suite 956 passed, coverage 95.70%; ruff, ruff format and strict mypy clean after final formatting fixes (focused provider and architecture tests re-run: 28 passed).
