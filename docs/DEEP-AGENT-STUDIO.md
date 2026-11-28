# Deep Agent Studio (development only)

A separate React app shows the project's coding Deep Agent working in real time:
- its transcript with tool-call cards;
- its live todo list;
- subagent cards;
- its structured result;
- a three-panel sandbox view (file tree, code, diff);
- token usage and the LLM cost in USD.

It follows the LangChain Deep Agents frontend patterns: overview, subagent streaming, todo list and sandbox.

## Architecture

```
deep-agent-ui (Vite + React, @langchain/react useStream)  ──SSE──▶  langgraph dev (Agent Server, :2024)
        │                                                              ├─ graph coding_agent    → CodingAgent IMPLEMENTATION phase
        └── GET /sandbox/{thread}/tree|file, POST /studio/cost ──▶     ├─ graph coding_planner  → CodingAgent PLANNING phase
                                                                       └─ http.app studio/api.py (read-only file browsing, pricing)
```

- **The agent itself:** `studio/agent_graph.py` serves the production `CodingAgent.build(...)` unchanged: same model factory, prompts, middleware and hardened `DockerSandbox`.
  - It has no network, a read-only root and a non-root user, and holds no secrets.
  - Planning uses a read-only mount.
  - Each run starts a sandbox container and removes it when the run ends.
- **Workspaces:** each thread gets its own workspace under `var/studio/workspaces`. It is seeded from `STUDIO_SEED_DIR`, which defaults to `tests/fixtures/sample_service`.
  - The workspace is found again after a server restart, because `langgraph dev` keeps threads on disk.
- **No subagents:** the subagent panel stays empty, because this project has one Deep Agent and the `task` tool is disabled. The card UI (`useMessages` / `useToolCalls` scoped to a subagent) renders any graph that does delegate.
- **Todos:** these come from `TodoListMiddleware`, which only the implementation phase has.
- **Cost:** computed from provider-reported `usage_metadata` and priced by the project's single price table through `POST /studio/cost`. This is LLM API cost only. Studio runs are **not** written to the worker's `llm_usage_records` ledger.
- **Scope:** the studio is not part of the Jira → draft-PR pipeline. It has no Jira or GitHub access. It reads (never writes) the pipeline's DynamoDB tables and S3 bucket through the application's ports (`studio/stores.py`); it runs no SQL.

## History

The **History** sidebar (toggled in the header) has two tabs. Both are read-only.
- **Studio runs:**
  - It lists every studio thread on the dev server with its prompt, assistant, outcome (implemented, blocked or plan), status, todo progress, model calls and LLM cost.
  - The cost is priced from the provider-reported usage of each message.
  - Clicking a run reopens it: transcript, todos, structured result and sandbox files.
  - The data comes from `GET /studio/threads`, which reads the Agent Server's own thread API.
- **Jira pipeline:**
  - It lists the worker's attempts, newest first, from the DynamoDB attempt listing (GSI2 `LIST#ATTEMPT`): issue, attempt number, repository, status and failure code.
  - Each attempt shows its cost from the usage ledger (`CostQuery`), broken down by stage, with token totals and models, plus a link to the draft PR when one exists (read from the attempt's `delivery_result` artifact through `ArtifactStore`, S3).
  - Attempts that ran before cost tracking existed show `n/a`, not $0.
  - The data comes from `GET /studio/attempts`. It needs the `ACCA_` AWS settings (`ACCA_AWS_ENDPOINT_URL` locally, the table names and the bucket); when the stores are unreachable the tab shows an error and the rest of the studio still works.

## Live Jira pipeline view

Use History → Jira pipeline, then click an attempt → **Watch live** (or **Open transcript** for a finished attempt).

- **What it shows:**
  - the attempt's progress through each stage (pending → … → completed, or the failure code);
  - LLM cost per stage with tokens, plus the draft PR link once it exists;
  - one tab per agent run (planning, implementation, correction) with the full transcript, tool calls, todos and the structured result;
  - the worker's live workspace files and diffs. These are visible only while a phase runs, because the worker deletes its workspace afterwards.
- **How it works:**
  - The worker checkpoints every agent step to DynamoDB (`DynamoDBSaver`, table `ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME`). When a planning or implementation run starts, it also indexes the run's thread id under the attempt (`ATTEMPT#<aid>/THREAD#<thread_id>`), because the checkpoint table cannot be searched by thread prefix. The index is best effort: a failure to write it is logged and never fails the phase.
  - `GET /studio/attempts/{id}/live` reads that index and rebuilds each phase's state from the checkpoints with the same agent graph, read-only (`aget_state` only).
  - The UI polls it every 3 s while the attempt is not finished, and the attempt list refreshes every 5 s, so new Stories appear on their own.
- **Freshness:** updates arrive per agent step, not token by token, because the worker runs in its own process and doesn't stream to the UI. The worker is unaffected.

### Story to PR, watched live

1. Start Docker Desktop, the `acca-api` container, `ngrok http 8000` (the Jira webhook points at the ngrok URL) and the worker: `caffeinate -i uv run ai-code-change worker`.
2. Start the Agent Server and the UI (below).
3. Create a Jira Story in a mapped project whose description names a registered repository, for example `Repository: <owner>/<repo>`.
4. Within a few seconds the attempt appears under Jira pipeline. Click **Watch live**.

## Run

```bash
# 1. Agent Server: --no-reload because the agent writes files under var/, which would trigger a reload
GRPC_VERBOSITY=ERROR GRPC_ENABLE_FORK_SUPPORT=false \
  uv run langgraph dev --no-browser --no-reload --port 2024

# 2. UI
cd deep-agent-ui && npm install && npm run dev   # http://127.0.0.1:5173
```

- **Requirements:** Docker running, the execution image `WORKSPACE_IMAGE` built, and `.env` with an LLM provider. Every run makes real, billed model calls.
- **Options:** pick the assistant (implementation or planning) in the header. "New thread" starts a fresh workspace. The URL hash keeps the thread, so a reload rejoins it.
- **`GRPC_*` variables:** without them, gRPC fork warnings from the Agent Server leak into sandbox command output.
- **Other settings:** `VITE_AGENT_URL` points the UI at another server. `STUDIO_SEED_DIR` seeds workspaces from another local directory.

## Verified (2026-11-26, claude-haiku-5-5)

| Run | Calls | Outcome | Tests | Cost |
|---|---:|---|---|---:|
| Implementation, "add farewell(name) with a test" | 7 | `implemented`: `app/greeting.py` and `tests/test_greeting.py` modified, diff shown in the UI | `python -m pytest -q` in the sandbox: 2 passed | $0.0067 |
| Planning | - | structured `ImplementationPlan` | a write to `/workspace` was refused (read-only) | - |

No sandbox containers were left behind after either run.
