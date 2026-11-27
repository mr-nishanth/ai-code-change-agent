# Configuration (environment contract)

Settings are defined in `src/ai_code_change_agent/core/config.py` (Pydantic Settings). They are read from the environment or `.env`, with no prefix, except the AWS settings, which carry `ACCA_` ([AWS services](#aws-services-dynamodb-sqs-s3)). `.env.example` lists every variable; `tests/unit/test_settings_docs.py` checks that every `ACCA_` setting appears in both files.

- **Secrets** are `SecretStr` and never logged.
- **The GitHub App private key** is referenced by absolute path only.
- **Fail-fast rules:**
  - partial GitHub App or Jira REST configuration is refused;
  - `GITHUB_PR_CREATE_ENABLED` and `JIRA_REPORTING_ENABLED` require their credentials;
  - `JOB_HEARTBEAT_SECONDS` must be at most half of `JOB_LEASE_SECONDS`;
  - `JOB_SWEEP_SECONDS` must be at least `JOB_RELAY_SECONDS`;
  - staging and production require `JIRA_WEBHOOK_SECRET`, `JIRA_PROJECT_KEYS` and `LLM_PROVIDER=bedrock`, and refuse `ACCA_AWS_ENDPOINT_URL` and `ACCA_BEDROCK_ENDPOINT_URL`;
  - development and test require `ACCA_AWS_ENDPOINT_URL` (the local Floci endpoint), staging and production must not set it (see [AWS services](#aws-services-dynamodb-sqs-s3)); both violations raise `ConfigurationError`.

| Variable | Default | Secret | Used by |
|---|---|---|---|
| APP_ENV | development | | all (`development`, `test`, `staging`, `production`) |
| APP_NAME, APP_HOST, APP_PORT | ai-code-change-agent, 127.0.0.1, 8000 | | API |
| LOG_LEVEL, LOG_JSON | INFO, true | | all |
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
| LLM_PROVIDER | anthropic | | worker. Two providers only: `anthropic` (Claude API, local development and tests) and `bedrock` (AWS Bedrock; **required** when APP_ENV is staging or production, which refuse `anthropic`). Locally `bedrock` can run through the Floci proxy with `ACCA_BEDROCK_ENDPOINT_URL` |
| LLM_MODEL | - | | worker (Anthropic model id such as `claude-haiku-5-5`, or a Bedrock model / inference-profile id) |
| ANTHROPIC_API_KEY | - | **yes** | worker (anthropic only) |
| BEDROCK_REGION | - | | worker (bedrock only, e.g. `us-east-1`) |
| BEDROCK_CREDENTIALS_PROFILE | - | | worker (bedrock only, optional). AWS credentials come from the worker's standard AWS chain (IAM role, profile or environment) and are never read from `.env` by Settings; the API container and sandboxes receive none. Refused together with `ACCA_BEDROCK_ENDPOINT_URL` |
| ACCA_BEDROCK_ENDPOINT_URL | - | | worker (bedrock only; **development and test only**). The Floci Bedrock Runtime, whose `proxy` backend forwards Converse to Anthropic's OpenAI-compatible endpoint: `http://127.0.0.1:4566` from the host. Same guard as `ACCA_AWS_ENDPOINT_URL`; needs `LLM_PROVIDER=bedrock`; the client then uses the dummy credentials `test`/`test`. Unset, `bedrock` is real AWS Bedrock. Usage rows are recorded as provider `bedrock-floci`. A proxied call is a real, paid model call (owner authorisation) |
| LLM_REQUEST_TIMEOUT_SECONDS, LLM_MAX_RETRIES, LLM_MAX_OUTPUT_TOKENS | 180, 2, 8192 | | worker |
| DEEP_AGENT_RECURSION_LIMIT | 250 | | worker |
| DEEP_AGENT_EXECUTE_TIMEOUT_SECONDS | 300 | | worker (per sandbox command) |
| WORKSPACE_ROOT, ARTIFACT_ROOT | - | | worker (absolute paths) |
| WORKSPACE_IMAGE | - | | worker (execution image) |
| WORKSPACE_CPU_LIMIT, WORKSPACE_MEMORY_LIMIT_MB, WORKSPACE_PIDS_LIMIT, WORKSPACE_DISK_LIMIT_MB, WORKSPACE_TIMEOUT_SECONDS | 1.0, 2048, 256, 1024, 7200 | | worker |
| WORKSPACE_NETWORK_POLICY | none | | worker (only value) |
| ARTIFACT_MAX_BYTES, ARTIFACT_MAX_ATTEMPT_BYTES | 5 MiB, 100 MiB | | worker |
| MAX_CONCURRENT_JOBS, JOB_LEASE_SECONDS, JOB_HEARTBEAT_SECONDS, JOB_MAX_ATTEMPTS, MAX_REPAIR_ATTEMPTS | 2, 300, 60, 3, 1 | | worker, API (`JOB_MAX_ATTEMPTS`) |
| JOB_RELAY_SECONDS, JOB_SWEEP_SECONDS | 30 (1..3600), 300 (10..86400) | | worker dispatcher: outbox relay (fallback for unsent jobs) and lost-message sweep; each tick is one GSI3 Query (docs/aws/DYNAMODB-DATA-MODEL.md 13) |

Requested names that are **deliberately absent**, because nothing uses them:

- `REDIS_URL`: there is no Redis (decision C1, kept by ADR-015).
- `GITHUB_API_BASE_URL`: the API host is fixed in code (`https://api.github.com`), so configuration cannot redirect tokens.
- `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_WEBHOOK_SECRET`: the App does not use OAuth or receive webhooks.
- `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OLLAMA_*` and `NVIDIA_*`: only Anthropic and Bedrock are supported (decision C5, amended 2026-11-26).
- `DOCKER_HOST`: honoured from the process environment if set, never in settings.
- `DEEP_AGENT_MAX_ITERATIONS`: the SDK's limit is the recursion limit.
- `LANGGRAPH_CHECKPOINT_CONFIGURATION`: checkpoints are configured by `ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME`, `ACCA_S3_BUCKET_NAME` and `ACCA_CHECKPOINT_TTL_DAYS`.


## AWS services (DynamoDB, SQS, S3)

These variables carry the `ACCA_` prefix so the standard `AWS_*` variables of a shell (`AWS_PROFILE`, `AWS_REGION`, `AWS_ENDPOINT_URL*`) never configure the application. Clients are built only in `integrations/aws/clients.py` (`AwsClients`: one `boto3` session, clients `dynamodb`, `sqs` and `s3` created on first use, `connect_timeout` 5 s, `read_timeout` 60 s, standard retry mode, `ignore_configured_endpoint_urls=True`). The API uses only DynamoDB (the application table); the worker and the CLI use all three ([ADR-015](decisions/ADR-015-dynamodb-sqs-s3-persistence.md)). Create the local resources with `sh infra/floci/init/bootstrap.sh`.

| Variable | Default | Secret | Notes |
|---|---|---|---|
| ACCA_AWS_REGION | us-east-1 | | AWS region name |
| ACCA_AWS_ENDPOINT_URL | - | | **Required in development and test**, refused in staging and production. Only `http://` on a loopback host (`127.0.0.1`, `localhost`, `[::1]`) or a single-label Docker host name, with an optional port and no path, query or credentials: host processes use `http://127.0.0.1:4566`, containers on the `acca-floci` network `http://acca-floci:4566`. Dotted names (for example `*.amazonaws.com`, `*.amazonaws.com.cn`, `*.api.aws`) are refused, so development and tests can never fall through to real AWS. With it set the clients use the static credentials `test`/`test` and never read the default credential chain, `AWS_PROFILE` or the AWS config files |
| ACCA_DYNAMODB_TABLE_NAME | acca-local-app | | application table ([DYNAMODB-DATA-MODEL.md](aws/DYNAMODB-DATA-MODEL.md)); API, worker, CLI |
| ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME | acca-local-checkpoints | | LangGraph checkpoint table (`DynamoDBSaver`); worker, Studio |
| ACCA_SQS_JOB_QUEUE_NAME | acca-local-jobs | | job queue (standard queue; its URL is resolved once with `GetQueueUrl`) |
| ACCA_S3_BUCKET_NAME | acca-local-artifacts | | artifact bucket: `artifacts/` (write-once attempt artifacts) and `checkpoints/` (checkpoint payloads over 358,400 bytes) |
| ACCA_CHECKPOINT_TTL_DAYS | 30 (1..365) | | checkpoint retention: DynamoDB TTL and the S3 lifecycle rule for offloaded payloads |
| ACCA_JIRA_DELIVERY_RETENTION_DAYS | 30 (1..365) | | TTL of Jira delivery dedupe records (the Story guard is permanent) |

Without an endpoint (staging and production) the clients use the process's default AWS credential chain (for example an IAM role). `JOB_LEASE_SECONDS` is capped at 3,600 s, below the SQS visibility-timeout limit of 43,200 s.

Not settings (shell or Compose variables, never read by `Settings`):

| Variable | Default | Used by |
|---|---|---|
| ACCA_FLOCI_ENDPOINT_URL | http://127.0.0.1:4566 | `tests/support/floci.py` and `infra/floci/init/*.sh` (loopback `http` only) |
| ACCA_FLOCI_INIT_DIR, ACCA_FLOCI_HEALTHY | (set by the scripts) | internal to `infra/floci/init/` |
| ACCA_DYNAMODB_RECREATE_ON_DRIFT | unset | `infra/floci/init/60-dynamodb.sh`: `1` deletes and recreates a local table whose indexes drifted from `infra/floci/schema` (its items are lost) |
| ACCA_API_CONTAINER_NAME, ACCA_API_PORT | acca-api, 8000 | `compose.yaml`: container name and loopback host port of the API (for a second stack, e.g. `acca-verify-api` on 18000) |

The Floci container itself is configured only with documented `FLOCI_*` variables in `infra/docker-compose.yml`.


## Automated pipeline

| Variable | Default | Secret | Used by |
|---|---|---|---|
| WORKFLOW_APPROVAL_MODE | automatic | | worker (`automatic`: policy decides PLAN and FINAL; `manual`: operator CLI) |
| DEPENDENCY_INDEX_URL | https://pypi.org/simple | | worker (the only index the resolver container may use; https, no credentials) |
| DEPENDENCY_MAX_PACKAGES, DEPENDENCY_MAX_BYTES, DEPENDENCY_TIMEOUT_SECONDS | 60, 314572800, 600 | | worker (per bundle) |
| DEPENDENCY_ALLOW_UNVERIFIED_INDEX | false | | worker (accept wheels whose sha256 the index does not publish; recorded as `unverified`; a mismatch always fails) |
| DEPENDENCY_RESOLVER_NETWORK | aica-dep-resolver | | worker (`--internal` Docker network of the resolver; created if missing, refused if routable; only exit is the allow-list proxy) |
| REFERENCE_SOURCES_FILE | config/reference_sources.yaml | | worker and API (version-controlled list of exact trusted documentation hosts for `search_docs` and `fetch_reference`; any path on a listed host is allowed, subdomains are not; a missing file disables both tools, an invalid one fails startup). Its optional `secondary_hosts` list (exact hosts, each also in `allowed_hosts`) only ranks those hosts' search hits after documentation. |
| REFERENCE_MAX_FETCHES, REFERENCE_MAX_BYTES, REFERENCE_TIMEOUT_SECONDS | 20, 1048576, 20 | | worker (per agent phase, per response, per request) |
| REFERENCE_MAX_URL_CHARS | 300 | | worker (50-2000; longest URL the agent may request from `fetch_reference`, stated in the tool description; `search_docs` drops longer hits) |
| REFERENCE_MAX_REDIRECTS | 3 | | worker (0-10; redirects `fetch_reference` follows, every hop re-checked) |
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

Usage is always recorded to the usage ledger in the DynamoDB application table (append-only items with uniqueness guards). Reports are printed by the worker and by `ai-code-change costs`. See [LLM-COST-TRACKING.md](LLM-COST-TRACKING.md). Model escalation (`LLM_FALLBACK_*`) is not implemented.


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


## Multi-repository, research and cost guards

| Variable | Default | Notes |
|---|---|---|
| MULTI_REPO_MAX_REPOSITORIES | 5 | Repositories per Story (2-20). See [MULTI-REPOSITORY.md](MULTI-REPOSITORY.md) |
| REFERENCE_SEARCH_ENABLED | true | `search_docs` (Anthropic web search, worker side) when the provider is anthropic |
| REFERENCE_MAX_SEARCHES | 5 | Searches per agent phase |
| REFERENCE_SEARCH_MAX_HITS | 8 | Hits returned per search (1-20) |
| DEEP_AGENT_MAX_MODEL_CALLS | 80 | Hard cap on model calls per agent phase run, for any model |
| LLM_MAX_COST_PER_PHASE_USD | 2.0 | The phase run stops before its next model call once this is spent. Bedrock ids use the matching Claude list price for this guard |

The model is never hard-coded: `LLM_MODEL` can be any Claude model on Anthropic (Haiku 5.5, Sonnet 5.5 and Opus 5.5 are priced) or any Bedrock model or inference-profile id.
