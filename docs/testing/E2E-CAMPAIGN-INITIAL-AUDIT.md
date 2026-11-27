# E2E campaign: initial audit

Audit date: 2026-11-27. Read-only, except restarting the local runtime after a reboot (Docker Desktop, `acca-postgres`, `acca-api`, ngrok), which follows docs/GETTING-STARTED.md. No Jira issue was created and no model was called.

## 1. Current system state

| Item | Observed |
|---|---|
| Git | Local branch `feature/s2112-edge-case-tests` = `feature/s2111-exact-reference-hosts` = `9df4fca`, clean, pushed. 9 commits ahead of `origin/main` (`9a31346`): the KAN-10 orchestration tests, the KAN-11 fixes (`6a874f3`), S2111 (`8ffbcfe`, `9df4fca`). No open PR on the agent repository; nothing of KAN-11 or S2111 is merged |
| API | `acca-api` healthy; `/health/ready` 200 locally and through `https://tying-spelling-customary.ngrok-free.dev`; image rebuilt from `9df4fca`'s tree, ships `config/reference_sources.yaml` |
| Worker | **Not running** (no process). No queued or running jobs, no `aica-sbx-*` sandboxes |
| PostgreSQL | `acca-postgres` healthy on 127.0.0.1:5435, Alembic at `0004 (head)` |
| Images | `ai-code-change-agent-api:latest`, `ai-code-change-agent/execution-python:0.2.0`, `execution-node:0.1.0`; resolver network `aica-dep-resolver` is `--internal` |
| Redis | Not part of the architecture (no usage in code or dependencies) |
| Model configuration | `anthropic` / `claude-haiku-5-5`; per phase 80 model calls and $2; 20 fetches and 5 searches per phase; job attempts 3; repair attempts 1; approvals `automatic`; PR creation, delivery and Jira reporting enabled; LangSmith off |
| Trusted documentation hosts (API and worker, identical) | docs.langchain.com, python.langchain.com, docs.python.org, fastapi.tiangolo.com, docs.pydantic.dev, pypi.org, raw.githubusercontent.com |

## 2. Jira

- Project KAN; issue types Epic, Subtask, Task, Story, Feature, Bug. KAN-1..KAN-11 exist; **KAN-12 returns 404** (KAN-12..15 unused so far).
- Webhook "AI Migration Agent TriggerNew Webhook Listener": enabled, `jira:issue_created`, filter `project = KAN AND issuetype = Story`, URL `…/webhooks/jira` on the ngrok host. **Every Story created in KAN starts a real pipeline run.**
- Deliveries KAN-7..KAN-11: all `VERIFIED` / `ACCEPTED`. Requests: KAN-7..10 `COMPLETED`, KAN-11 `FAILED` (`dependency_metadata_unavailable`, attempt 1, base `f883df7`).
- Jobs: intake 13 succeeded / 2 dead (earlier runs), child 8 succeeded, resume 2 succeeded.

## 3. GitHub

- App installation: selected repositories; contents write, pull requests write, metadata read; not suspended. Visible: automate_connector, autoSoft_connector, cdk_connector, reynolds_connector (all `master`, all ACTIVE in the registry and mapped to KAN).
- Open PRs: automate_connector #1 (SMOKE-1, draft), **#2 (KAN-7, not a draft)**; autoSoft_connector #1 (KAN-9, draft); reynolds_connector #1 (KAN-8, draft). KAN-10's three PRs are merged.
- Manifests on `master`: only automate_connector has `pyproject.toml` (httpx, pydantic, tenacity; test: pytest). reynolds_connector and autoSoft_connector have **no manifest**, so manifest discovery returns "none" there and only automate_connector exercises real dependency preparation.

## 4. S2111 state

| Check | Result |
|---|---|
| Exact-host matching, all paths, no implied subdomains, IDNA/case/trailing dot | Implemented in `core/reference_hosts.py`; shared by `search_docs` and `fetch_reference` |
| Redirects | Every hop re-checked (scheme, port, exact host, public addresses), at most 3 |
| Query-string and secret protection | Agent-requested URLs: no query string, at most 300 characters, no secret (`9df4fca`) |
| `REFERENCE_ALLOWED_DOMAINS` | Removed from code, configuration, docs and the local `.env` |
| `pydantic.dev` | **Not approved, not listed**; `docs.pydantic.dev` URLs therefore fail with `reference_redirect_not_allowed:pydantic.dev` |
| Live check (in-process, not production config) | Both Pydantic `llms.txt` indexes, a linked `index.md` page and the docs.pydantic.dev → pydantic.dev redirect work when both hosts are listed |
| Quality gates at `9df4fca` | ruff and format clean; strict mypy clean (296 files); pytest 1,338 passed, 0 failed, 0 skipped, coverage 90.51% (PostgreSQL and Docker tests included) |
| Merge | **Not merged; no PR opened** |

## 5. Missing automated coverage (mapped from the existing tests)

High risk first; all can be added as mocked tests in the existing pytest files:

1. Multi-repo: contract hash mismatch; invalid contract; an upstream child that FAILS (only the missing-contract path is asserted); three-level chain A → B → C.
2. LLM: retries cannot bypass the phase cost limit; provider 429; authentication failure; tool-call failure.
3. Validation: a validation timeout fails the attempt and publishes nothing; individual ruff / format / mypy failures.
4. GitHub: rate limit on write paths; PR-create timeout after GitHub created the PR (only a 404 on re-read is simulated).
5. Jira: parent report with several failed children; intake-level Jira timeout.
6. Recovery: API restart mid-request.

## 6. Blockers

1. S2111 not merged (needs a PR and your merge).
2. `pydantic.dev` not approved (KAN-12 research depends on it).
3. Worker not running (start after the merge so it loads the merged configuration).
4. Missing automated tests (section 5) are Phase 3 work.
5. KAN-14 and KAN-15 as *Stories* would each start a real agent run (see the webhook filter); see the proposal below.

## 7. Proposed Stories (draft, not created)

### KAN-12 [E2E TEST] Verify Complete Single-Repository AI Code Change Pipeline (Story)
Repository: automate_connector. Gap found in the source: `VehicleRecord.vin` uses `^[A-Z0-9]{17}$`, so it accepts I, O and Q, which the backend (`reynolds_connector/api/vin.py`, `^[A-HJ-NPR-Z0-9]{17}$`) rejects; the backend also strips whitespace and upper-cases. Task: align `VehicleRecord.vin` with the backend rule using a Pydantic v2 `field_validator` (mode `before` for normalisation), consulting the current official Pydantic validator documentation; add tests (lowercase/padded accepted and normalised; I/O/Q, wrong length, symbols rejected); no public interface change. Validation: `python -m pytest -q`. Scope: `src/automate_connector/reynolds/models.py`, `tests/test_models.py`.

### KAN-13 [E2E TEST] Verify Multi-Repository Dependency Execution and Contract Handoffs (Story)
Repositories, upstream first: reynolds_connector → automate_connector → autoSoft_connector. Candidate genuine contract change: **structured error contract**. Today the backend returns `{"detail": …}` for 404 and FastAPI's default body for 422; the client maps 422 to a generic `ReynoldsError` (no typed invalid-VIN error); autoSoft propagates every client error unchanged. Change: backend publishes `ErrorResponse{code, detail}` with codes `vehicle_not_found` / `vin_invalid` for 404 and 422; client raises `InvalidVinError` / `VehicleNotFoundError` from `code`; autoSoft's `sync_vehicle` maps them to its own documented errors. Each repository changes, and the contract (`ErrorResponse`) is the hand-off. **Needs your confirmation that this behaviour is wanted**; if not, the honest result is that no genuine three-repository change exists now. Run it after KAN-12's PR is merged (both touch automate_connector).

### KAN-14 [E2E TEST] Verify Execution Failure Handling, Retry and Recovery
### KAN-15 [E2E TEST] Verify AI Agent Security, Repository Authorization and Sandbox Boundaries
Recommendation: create both as **Task** issues (the webhook only fires for Stories), used as tracking tickets for the automated fault-injection and security evidence. As Stories they would start real agent runs with no code change to make. The scenarios themselves run as mocked/isolated tests (Phase 3), plus non-destructive live probes (bad signature and duplicate delivery against the webhook, refused fetches, sandbox checks).

## 8. Approvals required

| Gate | Request |
|---|---|
| S2111 | Open PR `feature/s2111-exact-reference-hosts` → `main` (you merge) |
| Trust | Add `pydantic.dev` to `config/reference_sources.yaml` (keep `docs.pydantic.dev`) |
| A | The four issue texts above, and KAN-14/15 as Task issues |
| B | Real model use and campaign budget (below) |

## 9. Model usage and budget (to approve at Gate B)

Configuration is the current one (verified above). Limits per phase: 80 calls, $2. One attempt has up to 3 cost-guarded phases (plan, implement, repair) = $6; intake jobs retry up to 3 times = $18 per repository attempt chain.

| Run | Real executions | Theoretical maximum | Expected (history: KAN-7 $0.02, KAN-10 $0.027 for 3 repos) |
|---|---|---|---|
| KAN-12 | 1 repository | $18 | < $0.10 |
| KAN-13 | 3 children | $54 | < $0.20 |
| KAN-14, KAN-15 | none (mocked) | $0 | $0 |

There is no campaign-level cap in code; proposed operator cap: **$5 total**, checked with `ai-code-change costs` after each run, stopping the worker if exceeded.
