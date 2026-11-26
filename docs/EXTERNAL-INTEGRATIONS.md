# External integrations

| Integration | Reused | Configured in the new `.env` | Verified in this project | Still required |
|---|---|---|---|---|
| Jira Cloud site (`JIRA_BASE_URL`, projects `JIRA_PROJECT_KEYS`) | Yes (same site, account, token and webhook secret) | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_WEBHOOK_SECRET`, `JIRA_PROJECT_KEYS` (migrated) | Unit and integration tests with doubles; local signature check | Point the Jira webhook URL at `/webhooks/jira` when deployed; verify a real delivery; verify **Add Comments** permission before enabling reporting |
| GitHub App "AI Code Change Agent" (`GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`) | Yes (no new App, no setting changed) | `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH` (migrated; key file validated locally) | Unit tests and fake GitHub backed by real `git`; no real call from this project | Register repositories (`--from-provider`); a live read when authorised; a live draft PR only with explicit authorisation |
| Anthropic API | Key migrated from the old `.env` (`ANTHROPIC_API_KEY`, model from `ANTHROPIC_MODEL` to `LLM_MODEL`) | yes | Presence only; `ChatAnthropic` and the Deep Agent build without a call | One real model call when you authorise it; whether the key is valid is unknown |
| Docker (local daemon) | Same host daemon | `WORKSPACE_*` | Real: sandbox isolation, agent-in-sandbox, workspace tests; execution image built | Production sandbox backend (see ARCHITECTURE.md) |
| PostgreSQL | New instance (container `acca-postgres`, port 5435, volume `acca_pgdata`) | `DATABASE_URL` (new local credentials) | Real: migrations, triggers, queue, checkpointer | none |


## Added in S2041-S2043

| Integration | Use | Configuration | Verified |
|---|---|---|---|
| Anthropic Claude API (`claude-haiku-5-5`) | LLM for the Deep Agent in local development and tests (`LLM_PROVIDER=anthropic`) | `ANTHROPIC_API_KEY` [secret], `LLM_MODEL` | Live: KAN-7 end to end, draft PR #2 (S2044) |
| AWS Bedrock | LLM for staging/production (`LLM_PROVIDER=bedrock`, required there) | `BEDROCK_REGION`, AWS credential chain | Unit-tested only |
| PyPI (`DEPENDENCY_INDEX_URL`) | Wheels for dependency bundles (resolver container only) | `DEPENDENCY_*` | Live: `six==1.17.0` bundle |
| Documentation sites | `fetch_reference` tool (worker side) | `REFERENCE_ALLOWED_DOMAINS` | Unit-tested with a mock transport |
| GitHub App | Draft PR delivery (create-only refs, never merge) | `CODE_CHANGE_DELIVERY_ENABLED`, `GITHUB_PR_CREATE_ENABLED` | see KAN-7 run |
| Jira | Outcome comments (success and failure) | `JIRA_REPORTING_ENABLED` (Add Comments permission verified present) | see KAN-7 run |

Removed on 2026-11-26: the NVIDIA API catalog and local Ollama providers (S2042-S2043 experiments). Only Anthropic and Bedrock remain.
