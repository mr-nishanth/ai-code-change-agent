# External integrations

| Integration | Used for | Configuration | Verified |
|---|---|---|---|
| Jira Cloud | Story intake (signed webhook), Story reads, one outcome comment per Story or report round | `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` [secret], `JIRA_WEBHOOK_SECRET` [secret], `JIRA_PROJECT_KEYS`, `JIRA_REPORTING_ENABLED` | Live: KAN-7, KAN-8, KAN-9 |
| GitHub App | Repository identity, pinned baselines, draft PR delivery (create-only, never merge) | `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, `CODE_CHANGE_DELIVERY_ENABLED`, `GITHUB_PR_CREATE_ENABLED` | Live: draft PRs on four repositories |
| Anthropic Claude API | LLM for local development and tests; worker-side documentation search | `LLM_PROVIDER=anthropic`, `LLM_MODEL`, `ANTHROPIC_API_KEY` [secret] | Live with claude-haiku-5-5 |
| AWS Bedrock | LLM for staging and production (required there) | `LLM_PROVIDER=bedrock`, `LLM_MODEL`, `BEDROCK_REGION`, AWS credential chain | Unit-tested |
| PyPI (`DEPENDENCY_INDEX_URL`) | Hash-locked wheels for dependency bundles (resolver container only) | `DEPENDENCY_*` | Live: one bundle |
| Documentation sites | `search_docs` / `fetch_reference`, run in the worker | `config/reference_sources.yaml`, `REFERENCE_*` | Search live; fetch mock-tested |
| Docker | Sandboxes and validation containers (offline, hardened) | `WORKSPACE_*` | Docker integration tests |
| PostgreSQL | All state, job queue, checkpoints, cost ledger | `DATABASE_URL` | Integration tests |
| LangSmith (optional) | Tracing (off by default) | `LANGSMITH_*` | Configuration tests |

Setup guides: [SETUP-GITHUB-APP.md](SETUP-GITHUB-APP.md) and [SETUP-JIRA-WEBHOOK.md](SETUP-JIRA-WEBHOOK.md).
