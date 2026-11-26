# Configuration (environment contract)

Settings are defined in `src/ai_code_change_agent/core/config.py` (Pydantic Settings). They are read from the environment or `.env`, with no prefix. `.env.example` lists every variable.

- **Secrets** are `SecretStr` and never logged.
- **The GitHub App private key** is referenced by absolute path only.
- **Fail-fast rules:**
  - partial GitHub App or Jira REST configuration is refused;
  - `GITHUB_PR_CREATE_ENABLED` and `JIRA_REPORTING_ENABLED` require their credentials;
  - `JOB_HEARTBEAT_SECONDS` must be at most half of `JOB_LEASE_SECONDS`;
  - staging and production require `DATABASE_URL`, `JIRA_WEBHOOK_SECRET`, `JIRA_PROJECT_KEYS` and `LLM_PROVIDER=bedrock`.

| Variable | Default | Secret | Used by |
|---|---|---|---|
| APP_ENV | development | | all (`development`, `test`, `staging`, `production`) |
| APP_NAME, APP_HOST, APP_PORT | ai-code-change-agent, 127.0.0.1, 8000 | | API |
| LOG_LEVEL, LOG_JSON | INFO, true | | all |
| DATABASE_URL | - | yes (password) | API, worker, CLI, Alembic |
| LANGGRAPH_CHECKPOINT_SCHEMA | langgraph | | worker |
| JIRA_BASE_URL, JIRA_EMAIL | - | | worker (read, report) |
| JIRA_API_TOKEN | - | **yes** | worker |
| JIRA_WEBHOOK_SECRET | - | **yes** | API |
| JIRA_PROJECT_KEYS | (empty) | | API (webhook scope) |
| JIRA_STORY_ISSUE_TYPES | Story | | API |
| JIRA_WEBHOOK_EVENT | jira:issue_created | | API |
| JIRA_WEBHOOK_MAX_BODY_BYTES | 1048576 | | API |
| JIRA_REPOSITORY_FIELD | - | | worker (custom field with the repository reference; without it the project mapping decides) |
| JIRA_CONNECT/READ_TIMEOUT_SECONDS, JIRA_MAX_RETRIES, JIRA_MAX_RESPONSE_BYTES, JIRA_COMMENT_PAGE_SIZE, JIRA_MAX_COMMENTS | bounded defaults | | worker |
| JIRA_REPORTING_ENABLED | false | | worker |
| GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID | - | | worker, CLI |
| GITHUB_APP_PRIVATE_KEY_PATH | - | path to a secret | worker, CLI |
| CODE_CHANGE_DELIVERY_ENABLED, GITHUB_PR_CREATE_ENABLED | false, false | | worker (both needed to publish) |
| LLM_PROVIDER | anthropic | | worker. Two providers only: `anthropic` (Claude API, local development and tests) and `bedrock` (AWS Bedrock; **required** when APP_ENV is staging or production, which refuse `anthropic`) |
| LLM_MODEL | - | | worker (Anthropic model id such as `claude-haiku-5-5`, or a Bedrock model / inference-profile id) |
| ANTHROPIC_API_KEY | - | **yes** | worker (anthropic only) |
| BEDROCK_REGION | - | | worker (bedrock only, e.g. `us-east-1`) |
| BEDROCK_CREDENTIALS_PROFILE | - | | worker (bedrock only, optional). AWS credentials come from the worker's standard AWS chain (IAM role, profile or environment) and are never read from `.env` by Settings; the API container and sandboxes receive none |
| LLM_REQUEST_TIMEOUT_SECONDS, LLM_MAX_RETRIES, LLM_MAX_OUTPUT_TOKENS | 180, 2, 8192 | | worker |
| DEEP_AGENT_RECURSION_LIMIT | 250 | | worker |
| DEEP_AGENT_EXECUTE_TIMEOUT_SECONDS | 300 | | worker (per sandbox command) |
| WORKSPACE_ROOT, ARTIFACT_ROOT | - | | worker (absolute paths) |
| WORKSPACE_IMAGE | - | | worker (execution image) |
| WORKSPACE_CPU_LIMIT, WORKSPACE_MEMORY_LIMIT_MB, WORKSPACE_PIDS_LIMIT, WORKSPACE_DISK_LIMIT_MB, WORKSPACE_TIMEOUT_SECONDS | 1.0, 2048, 256, 1024, 7200 | | worker |
| WORKSPACE_NETWORK_POLICY | none | | worker (only value) |
| ARTIFACT_MAX_BYTES, ARTIFACT_MAX_ATTEMPT_BYTES | 5 MiB, 100 MiB | | worker |
| MAX_CONCURRENT_JOBS, JOB_LEASE_SECONDS, JOB_HEARTBEAT_SECONDS, JOB_MAX_ATTEMPTS, MAX_REPAIR_ATTEMPTS | 2, 300, 60, 3, 1 | | worker, API (`JOB_MAX_ATTEMPTS`) |

Requested names that are **deliberately absent**, because nothing uses them:

- `REDIS_URL` (decision C1).
- `GITHUB_API_BASE_URL`: the API host is fixed in code (`https://api.github.com`), so configuration cannot redirect tokens.
- `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_WEBHOOK_SECRET`: the App does not use OAuth or receive webhooks.
- `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OLLAMA_*` and `NVIDIA_*`: only Anthropic and Bedrock are supported (decision C5, amended 2026-11-26).
- `DOCKER_HOST`: honoured from the process environment if set, never in settings.
- `DEEP_AGENT_MAX_ITERATIONS`: the SDK's limit is the recursion limit.
- `LANGGRAPH_CHECKPOINT_CONFIGURATION`: replaced by `LANGGRAPH_CHECKPOINT_SCHEMA`.


## Automated pipeline

| Variable | Default | Secret | Used by |
|---|---|---|---|
| WORKFLOW_APPROVAL_MODE | automatic | | worker (`automatic`: policy decides PLAN and FINAL; `manual`: operator CLI) |
| DEPENDENCY_INDEX_URL | https://pypi.org/simple | | worker (the only index the resolver container may use; https, no credentials) |
| DEPENDENCY_MAX_PACKAGES, DEPENDENCY_MAX_BYTES, DEPENDENCY_TIMEOUT_SECONDS | 60, 314572800, 600 | | worker (per bundle) |
| REFERENCE_ALLOWED_DOMAINS | (empty: tool disabled) | | worker (comma-separated domains for `fetch_reference`) |
| REFERENCE_MAX_FETCHES, REFERENCE_MAX_BYTES, REFERENCE_TIMEOUT_SECONDS | 20, 1048576, 20 | | worker (per agent phase, per response, per request) |
| DEEP_AGENT_RUN_TIMEOUT_SECONDS | 5400 | | worker (wall-clock budget per agent phase) |

The API container needs none of these. It still receives only the webhook settings, so API and worker can't disagree on pipeline behaviour.


## LangSmith tracing (worker only)

| Variable | Default | Secret | Notes |
|---|---|---|---|
| LANGSMITH_TRACING | false | | Deep Agents, LangChain and LangGraph runs trace automatically when true |
| LANGSMITH_API_KEY | - | **yes** | required when tracing |
| LANGSMITH_ENDPOINT | (US cloud) | | the API URL for your region or self-hosted instance (https) |
| LANGSMITH_WORKSPACE_ID | - | | needed when the key can reach several workspaces |
| LANGSMITH_AGENT_ID, LANGSMITH_AGENT_ENVIRONMENT | -, local | | agent-based workspace destination |
| LANGSMITH_PROJECT | APP_NAME | | project-based workspace destination; never together with an agent id |
| LANGSMITH_HIDE_INPUTS, LANGSMITH_HIDE_OUTPUTS | false | | set true to keep Story and repository content out of LangSmith |

The worker exports exactly these variables at start-up (`core/tracing.py`), removes legacy `LANGCHAIN_PROJECT`/`LANGCHAIN_SESSION` destinations and flushes traces at shutdown. Agent runs are named `coding-agent-<phase>` and carry `thread_id` (`attempt:<id>:<phase>`), `phase` metadata and tags, which child runs inherit. The API process never traces.


## LLM cost tracking

| Variable | Default | Notes |
|---|---|---|
| LLM_PRICING_FILE | - | optional absolute path to a JSON price list that overrides the built-in table (`application/costs/pricing.py`, version `anthropic-api-2026-11-25`) |

Usage is always recorded to `llm_usage_records` (migration 0002). Reports are printed by the worker and by `ai-code-change costs`. See [LLM-COST-TRACKING.md](LLM-COST-TRACKING.md). Model escalation (`LLM_FALLBACK_*`) is not implemented.


## Agent context and pacing

| Variable | Default | Notes |
|---|---|---|
| LLM_CONTEXT_TOKENS | 128000 | Set as the model profile's `max_input_tokens`. Deep Agents summarises at 85% and keeps 10%; the agent clears old tool outputs (keeping the last five) at 50%. |
| LLM_REQUESTS_PER_MINUTE | (unlimited) | `InMemoryRateLimiter` on the model, which spaces calls out before the provider rate-limits |

Agent harness (from the Deep Agents docs review):
- Deep Agents' own files (`/large_tool_results/`, `/conversation_history/`) are routed to graph state through a `CompositeBackend`; commands and repository files stay in the sandbox.
- There is no implicit general-purpose subagent: the `task` tool is disabled through a harness profile.
- `write_todos` is available in the implementation phase only.
- Every phase run gets a fresh agent thread, so a crash re-run never appends to stale history.


## Multi-repository, toolchains, research and cost guards

| Variable | Default | Notes |
|---|---|---|
| MULTI_REPO_MAX_REPOSITORIES | 5 | Repositories per Story (2-20). See [MULTI-REPOSITORY.md](MULTI-REPOSITORY.md) |
| WORKSPACE_IMAGES | (empty) | Further toolchain images as `family=image` pairs, e.g. `node=ai-code-change-agent/execution-node:0.1.0`. The python image stays `WORKSPACE_IMAGE` |
| REFERENCE_SEARCH_ENABLED | true | `search_docs` (Anthropic web search, worker side) when the provider is anthropic |
| REFERENCE_MAX_SEARCHES | 5 | Searches per agent phase |
| DEEP_AGENT_MAX_MODEL_CALLS | 80 | Hard cap on model calls per agent phase run, for any model |
| LLM_MAX_COST_PER_PHASE_USD | 2.0 | The phase run stops before its next model call once this is spent. Bedrock ids use the matching Claude list price for this guard |

The model is never hard-coded: `LLM_MODEL` can be any Claude model on Anthropic (Haiku 5.5, Sonnet 5.5 and Opus 5.5 are priced) or any Bedrock model or inference-profile id.
