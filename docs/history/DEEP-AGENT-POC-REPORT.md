# Deep Agent POC report (S2041)

> **Historical record.** Repository names, ids and commands below are from that run. Onboarding is now generic: `register --from-provider <owner>/<repo> --project <KEY> --enable` (see AUTOMATED-PIPELINE.md); no code or document is specific to a repository.

> **Historical report.** Since 2026-11-26 the only LLM providers are `anthropic` (local development and tests) and `bedrock` (staging and production). The Ollama and NVIDIA providers described below were removed. See [CONFIGURATION.md](CONFIGURATION.md).

Date: 2026-11-25. Story: **KAN-7** "S2041 – Create a Basic FastAPI Server" (Target Repository: `automate_connector`). Read-only: the run stopped at the PLAN gate. Delivery (`CODE_CHANGE_DELIVERY_ENABLED`, `GITHUB_PR_CREATE_ENABLED`) and `JIRA_REPORTING_ENABLED` were **false** in `.env` and in the worker's effective settings before any agent run. The worker was restarted after each configuration or code change.

## A. Runtime verification

| Service | State |
|---|---|
| PostgreSQL `acca-postgres` (127.0.0.1:5435) | healthy |
| API `acca-api` (127.0.0.1:8000) | `/health/ready` 200; unsigned webhook via ngrok returns 401 |
| ngrok `tying-spelling-customary.ngrok-free.dev` → :8000 | matches the Jira webhook URL (`/webhooks/jira`), not modified |
| Worker (`uv run ai-code-change worker`) | running; processed KAN-7 |
| Execution image `ai-code-change-agent/execution-python:0.1.0` | present |
| Effective LLM config | `LLM_PROVIDER=nvidia`, `LLM_MODEL=moonshotai/kimi-k3.1-pro`, `NVIDIA_REASONING_EFFORT=low`, `LLM_REQUEST_TIMEOUT_SECONDS=600`, `LLM_MAX_RETRIES=4` |

Real pipeline path proven: Jira webhook → signed intake → `jira_deliveries`/request/job → worker → Jira `get_issue` 200 → repository resolved from the Story marker → pinned baseline workspace (`7708e5daaed0`, 1 file) → read-only planning sandbox → Deep Agent → plan artifact → `AWAITING_PLAN_APPROVAL`.

## B. Model verification

| Model | Text | Tool call | Deep Agent planning | Verdict |
|---|---|---|---|---|
| Ollama `qwen3-coder` (30B Q4, local) | OK | OK (242 s, valid `read_file`) | attempts 1-2: `agent_error_readtimeout`; no turn completed | not viable on this 24 GiB Mac |
| NVIDIA `moonshotai/kimi-k3.1-pro` (free tier) | OK (151-185 s) | OK (150-165 s, valid `read_file`) | attempts 3-4 failed (504, then 400); **attempt 5 passed** after fixes (16 min) | works; slow (free-tier latency) |

## C. Agent verification (attempt 5, `c6139a8c-bf9a-423a-b5df-ecaf4303d71e`)

Tool calls recorded in the agent's checkpoint:

| Step | Tool | Arguments | Result |
|---|---|---|---|
| 1 | `ls` | `{"path": "/workspace"}` | `['/workspace/README.md']` |
| 2 | `read_file` | `{"file_path": "/workspace/README.md"}` | lines 1-100 of 108 |
| 3 | `read_file` | `{..., "offset": 100, "limit": 20}` | lines 101-108 (correct paging) |
| 4 | `ImplementationPlan` | structured output | plan sha256 `28dec3b9356c...` |

- Valid arguments, correct handling of tool results, no recursion-limit or timeout failure, and no write, edit, delete or execute calls.
- **No hallucination:** findings state that the repository contains only `README.md`, with no application, dependencies or tests.
- Plan: create `pyproject.toml`, `app/{__init__,models,main}.py`, `app/api/routes/items.py` (+ package files) and `tests/test_api.py`; modify `README.md`. It covers acceptance criteria for all 5 endpoints, 201/404/422 behaviour, risks (in-memory store, test ordering, httpx test-only) and a TODO list.

## D. Repository

The installation's repositories were discovered dynamically: `automate_connector`, `autoSoft_connector`, `cdk_connector`, `reynolds_connector` (all `master`, README only). The Story selected `automate_connector` → registry key `automate-connector` (ACTIVE, mapped to KAN, profile v1 published with no commands). No registration or mapping was changed in this task.

## E. Security and zero-mutation verification

- Live sandbox: `--network none`, read-only root filesystem, `/workspace` mounted `rw=false`, user `501:20`, `CapDrop ALL`, `no-new-privileges`, pids 256, memory 2 GiB. No Docker socket, no credentials.
- Agent tools: only the SDK's sandbox-backed tools; no GitHub or Jira access. Publishing stays only in `publisher.py`, which is disabled.
- After the run:
  - GitHub: all four `master` heads are unchanged (`7708e5daaed0`, `1f6865517a3f`, `ede13b4bc5a4`, `45c612249a89`); no new branch; the only PR is `automate_connector` #1, still a draft, `updated_at` unchanged (2026-11-25T04:59:30Z).
  - Jira KAN-7: status To Do, 0 comments, last updated at creation.
  - Workspace: destroyed after the step; no sandbox containers left.
- NVIDIA is a third-party hosted service: the Story text and the repository content the agent reads are sent to it.

## F. Quality gates

970 passed, coverage 95.75% (baseline before this task: 958 / 95.71%). `ruff check`, `ruff format --check` and strict `mypy` are clean.

## G. Defects found and fixed (all in `providers/llm/factory.py` and `core/config.py`, with tests)

1. **Ollama context truncation:** Ollama's default 4096-token context silently truncated the Deep Agent prompt. Added `OLLAMA_NUM_CTX` (default 16384).
2. **New `nvidia` provider** (`ChatNVIDIA`, `NVIDIA_API_KEY`, optional `NVIDIA_REASONING_EFFORT`), with SDK confinement enforced by the architecture test.
3. **NVIDIA 60 s socket timeout:** the package default replaced by `LLM_REQUEST_TIMEOUT_SECONDS`.
4. **NVIDIA 504 gateway timeout** on long non-streamed reasoning responses: every call now streams.
5. **Free-tier transient failures** (429, 5xx, socket timeouts): bounded retries with capped exponential backoff (5 s, doubling, ≤ 60 s; `LLM_MAX_RETRIES`), only before any output has streamed.
6. **Tool-call history dropped (HTTP 400):** `langchain-nvidia-ai-endpoints` 1.5.0 serialises assistant tool calls only from `additional_kwargs`, while streamed messages carry them in `AIMessage.tool_calls`. The second turn therefore sent a tool result without its call. Fixed by re-serialising (copy, no mutation).

Also from earlier in the session: Story `Repository:` marker resolution and a test-left Docker container (removed).

## H. Findings, limitations and recommendation

- **Kimi via NVIDIA (free tier) is suitable for the next controlled experiment**: correct tool use, grounded plan, no mutations. It's slow (~2.5-3 min per call; 16 min for this 4-call plan), and implementation will need many more calls.
- **Blocker for implementing KAN-7:**
  - The execution image lacks `fastapi`, `uvicorn`, `pydantic` and `httpx`. The sandbox has no network (by design), so the agent cannot run the planned tests.
  - The repository profile has no validation commands, so the validation engine would not run tests either.
  - Fix before approving the plan: an execution image with the FastAPI test stack, and a profile with `python -m pytest` as its `test` command.
- `qwen3-coder` locally: not viable on this hardware for the agent loop.
- LangGraph warns when it deserialises `ImplementationPlan` from a checkpoint ("will be blocked in a future version"). Register the type in `allowed_msgpack_modules` before upgrading LangGraph.
- Attempts 1-4 of KAN-7 are recorded as FAILED, attempt 5 awaits plan approval. Re-runs were queued manually as intake jobs; there is no operator `retry` command yet.

**Recommended next task:** build the FastAPI-capable execution image (offline wheels), publish a profile v2 for `automate-connector` with a pytest command, then approve the PLAN gate for attempt 5 with delivery still off, and review the implementation and validation result before deciding on delivery.
