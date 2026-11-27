# S2113 pre-merge audit

Audit date: 2026-11-27. The audit itself was read-only: no network, Jira, GitHub or model call was made, and only local tests were run. The implementation plan is kept outside the repository (session scratchpad, `s2113/plan.md`). **Status: every fix below is done**, one commit per task on `feature/s2112-edge-case-tests` (not pushed). The commits are listed in section 7, and the final gate results in section 8.

## 1. Initial state

| Item | Observed |
|---|---|
| Branch | `feature/s2112-edge-case-tests` at `a9d0706` (docs(testing): S2112 automated edge-case test results), clean, in sync with `origin/feature/s2112-edge-case-tests` |
| Ahead of `origin/main` (`9a31346`) | 21 commits (KAN-10/11, S2111, S2112); not merged, no PR |
| Last recorded gate (S2112 results, section 9) | 1483 passed, 1 xfailed, 1 warning; coverage 91.69%; ruff, format, strict mypy clean |
| Reproduced here | `uv run pytest tests/unit -m "not docker and not integration" --no-cov`: 1395 passed, 1 xfailed, **1 warning** (`coroutine 'ahandle_event' was never awaited`) |
| Alembic head | `0004` (no migration needed by any planned fix) |

## 2. Findings and proposed fixes

Line numbers are at `a9d0706`.

### F1: Untrusted-content boundary spoofing (S2112 N2). Done in `cd86dbb`

Evidence: fixed, guessable markers, and several external inputs with no marker at all.

| Location | External text | Marking today |
|---|---|---|
| `agents/coding/prompts.py:86-91` | Jira summary, description, acceptance criteria | fixed `(UNTRUSTED DATA BEGINS/ENDS)` |
| `prompts.py:80-84` | repository manifest requirements | none (PEP 508-validated) |
| `prompts.py:108-111` | `role`, `responsibility` parsed from the Story (`domain/changes/multirepo.py:84-86`) | **none** |
| `prompts.py:120-125` | upstream contract (written by another agent from repository content) | fixed `(CONTRACT BEGINS/ENDS)` |
| `prompts.py:142-146` | validation feedback, including repository test output (`validation/schemas.py:124-134`) | **none** |
| `agents/coding/tools.py:74-86` | search titles, page age, URLs | "UNTRUSTED DATA" header, no end marker; titles may contain newlines |
| `tools.py:131-136` | fetched page text; `content_type` sits **outside** the markers and is server-controlled (`integrations/web/fetcher.py:229-231` checks only the prefix) | fixed markers |
| `tools.py:60,65,126` | `REFUSED:` codes that can carry a redirect host (`fetcher.py:197`) or a provider error code | none |

Not in scope: `validation_commands` (published registry profile), repository keys (registry), the approved plan JSON (`prompts.py:139-140`: it is the instruction the agent carries out, it passed the policy gate, and JSON escaping prevents line spoofing), and SDK sandbox tool output (covered by the "repository content is DATA" rule and residual risk 5 in `docs/SECURITY.md`).

Fix: add one module, `agents/coding/untrusted.py`. `frame_untrusted(label, text)` wraps text in `UNTRUSTED DATA <id> BEGINS: <label>` / `UNTRUSTED DATA <id> ENDS`, with a fresh `secrets.token_hex(8)` id per call, regenerated if the text contains it. Two more helpers: `one_line()` collapses whitespace in titles and codes, and `safe_media_type()` restricts `content_type`. A new system-prompt rule explains that only the BEGINS/ENDS pair with the same id delimits data. All eight call sites switch to the helper, and fetch metadata moves inside the frame. Owner approval was requested in S2112 (prompt and tool format change); this audit lists it under "Needs approval" in the plan.

### F2: Streamed usage lost on error (S2112 N1). Done in `3a5b3f9`: strict xfail removed, non-streaming pinned

Evidence that it cannot happen in production:
- `providers/llm/factory.py:38-58` never sets `streaming` for either provider;
- the agent calls `agent.ainvoke` (`agents/coding/agent.py:188`); the parent graph also uses `ainvoke` (`orchestration/langgraph/runner.py:21,27`). There is no `astream` or `stream_mode` anywhere in `src`;
- LangChain streams only on a `stream` kwarg, `streaming=True` or a streaming callback handler (`langchain_core/language_models/chat_models.py:590-626`). None of these is present;
- the deepagents subagent `astream` is unused, because the general-purpose subagent is disabled (`agent.py:65-71`);
- the "buffered complete-or-retry streaming" was the NVIDIA wrapper, removed on 2026-11-26 (`docs/ARCHITECTURE-DECISIONS.md` C5).

Fix: delete `tests/unit/costs/test_costs.py::test_usage_streamed_before_a_failure_is_recorded_and_charged` (strict xfail, lines 487-509). Add a guard test that both production models report `_should_stream(...) is False` and do not set `streaming`. Document in `docs/LLM-COST-TRACKING.md` that, if streaming is ever enabled, `on_llm_error`'s `response` must be read in `UsageCallbackHandler` and `BudgetGuard` (`application/costs/usage.py:199-214, 249-264`), de-duplicated by `run_id`.

### F3: Validation infrastructure failures consume the repair run (S2112 N3). Done in `727662e`

Evidence:
- `orchestration/steps.py:210`: `INFRA_CODES = {provider_unavailable, rate_limited, provider_timeout}`, the Git-host codes only;
- `steps.py:689-697`: a run is treated as an infrastructure failure only if every failed check's `detail` is in that set. A Docker crash yields REBUILD_INTEGRITY `docker_unavailable` (`execution/docker/runtime.py:479-480`, `validation/engine.py:151-154`), which is not in the set, so it triggers a repair;
- `runtime.py:58,500-501`: 125, 126 **and 127** all map to `ERROR`/`container_start_failed`;
- `validation/test_runner.py:46-55` drops `CommandResult.reason`, and `CommandRecord` (`validation/schemas.py:45-53`) has no field for it, so the decision cannot tell infrastructure from code;
- pinned by `tests/unit/orchestration/test_automatic_workflow.py:411-433` (`docker_crash` and `executable_missing` both give `validation_failed`, after a repair).

Fix:
- 127 maps to `command_not_found`;
- `CommandRecord.reason` is added (optional, backward compatible) and copied by the test runner;
- in `validate`, an infrastructure code (Docker unavailable, container start failed 125/126, runtime failure, Git host) in a check detail or a command reason fails the attempt with the existing `validation_infrastructure_unavailable` code. There is no repair, and the report says it is retryable;
- `command_not_found` fails with `validation_command_not_found`. There is no repair, and the report says it is not retryable.

Timeouts stay code failures (repairable). The timeout is the profile's own per-command budget, the most common cause is the change itself, the repair is bounded to one run, and the existing semantics (`validation/decision.py:31-34`, `test_automatic_workflow.py:412,417`) already treat it so. The owner should confirm this choice.

### F4: Intake retries permanent Jira errors (S2112 N4). Done in `dede242`

Evidence:
- `orchestration/intake.py:63-66` catches only `JiraIssueNotFoundError`. Every other Jira error falls through to the worker's retry-by-default branch (`application/jobs/worker.py:110-123`), so 401, 403 and 400 are read up to 3 times;
- the errors already carry `retryable` (`application/jira/errors.py:10-76`; the client sets it per status in `integrations/jira/client.py:153-178`, and `_RETRYABLE_STATUSES = {500, 502, 503, 504}`);
- the job signals "not retryable" by raising `JobFailedError` (`application/jobs/queue.py:33-38`);
- `Retry-After` is honoured inside the client up to 30 s (`client.py:122-130`), but ignored by the job backoff.

Fix:
- in intake, `JiraIntegrationError` with `retryable=False` (400, 401, 403, other 4xx, 501/505, invalid or oversized responses) is turned into `JobFailedError(exc.code)`;
- 404 keeps its existing `jira_issue_not_found` semantics;
- transient errors (429, 5xx, timeouts, connection) propagate unchanged, keeping the class-name codes that `tests/integration/test_intake_jira_outage_db.py:133,157` pins;
- the worker raises the backoff to `ceil(retry_after_seconds)` when the exception carries one, capped at 900 s.

### F5: A permanent single-repository Jira comment failure is only logged (S2112 N5). Done in `d27771f`

Evidence:
- `integrations/jira/reporting.py:103-109` collapses PERMANENT and RETRYABLE into `ReportOutcome.FAILED`;
- `orchestration/steps.py:932-948` `_report_once` stores the `JIRA_REPORT` marker artifact only when the outcome is not `FAILED`, so a permanent failure leaves no record and a second `cleanup()` (`steps.py:882-892`) posts again;
- multi-repository parents already record `PERMANENT_FAILURE` in `code_change_parents.jira_report` (`persistence/models/multirepo.py:53-58`, migration 0004), but a single-repository attempt has no parent row.

Fix, with no migration: reuse the `JIRA_REPORT` artifact.
- Add `ReportOutcome.PERMANENT_FAILURE`; `report()` returns it for `CommentStatus.PERMANENT` (400/401/403/404/405/410/413/422, invalid key: `reporting.py:53`).
- `_report_once` then stores `JiraReportDoc(status, outcome="PERMANENT_FAILURE")`, so the comment is never re-posted.
- The attempt outcome is unchanged and nothing is re-implemented.
- Retryable failures still store nothing (existing behaviour).

### F6: Contract origin not verified; integrity failure reported as retryable (S2112 N6, N7). Done in `4c9298b`

Evidence:
- `application/multirepo/coordinator.py:466-478` `context_for` checks the hash and schema (`application/artifacts/hashing.py:42-61`) but never compares `ContractDoc.parent_id`, `repository_key` or `attempt_id` (`application/multirepo/models.py:94-105`) with the child records;
- `coordinator.py:360-362` prints `Retryable: yes (... retry-child ...)` for every unsuccessful child, including `artifact_*` integrity failures that fail again on retry.

Fix:
- after `load_verified`, require `doc.parent_id == child.parent_id`, `doc.repository_key == key` and `doc.attempt_id == up.attempt_id`. Otherwise raise `ArtifactIntegrityError("artifact_provenance_mismatch")` (`application/errors.py:19`). The dependent fails at planning, before any workspace or agent run;
- in the report, `artifact_*` codes print `Retryable: no (contract integrity failure: needs operator investigation before retry-child <id>)`. Child state and the retry command are unchanged.

## 3. Warnings: root cause

| Warning | Root cause | Fix (done in `238ae9d`) |
|---|---|---|
| `RuntimeWarning: coroutine 'ahandle_event' was never awaited`, reported in `tests/unit/jira/test_adf.py` in earlier runs and in `tests/unit/agents/test_coding_agent.py::test_a_provider_error_after_a_priced_call_keeps_the_recorded_usage` here | Created, never awaited, in `langchain_core/callbacks/manager.py:1973` (`AsyncCallbackManager.on_chat_model_start`; the tracemalloc allocation trace points there). `UsageTracker.handlers` (`application/costs/usage.py:332-350`) orders `[BudgetGuard, UsageCallbackHandler]`. When the guard raises `BudgetExceededError` (`usage.py:245-247`, `raise_error=True`), the usage handler's already-created coroutine is abandoned. The warning surfaces at the next garbage collection, so it is attributed to whatever test runs then. `test_adf.py` has no async code at all. Reproduced alone by `test_coding_agent.py::test_the_cost_guard_stops_the_run_even_while_output_retries_remain` | Return the guard **last** from `handlers()`. Add a test that trips the guard under `warnings.catch_warnings` plus `gc.collect()` and asserts no "never awaited" warning. Production effect today: none (the abandoned handler has no start hook logic), but the fix removes the leak |

There is only one warning source: the full unit run shows exactly 1 warning, and S2112 recorded 1.

## 4. Dead-code audit (after the 14 removals in S2112 section 7)

Method: every top-level and method definition in `src/`, `studio/` and `scripts/` was counted against `src`, `studio`, `scripts`, `tests`, the docs, `compose.yaml`, `docker/`, `langgraph.json` and `pyproject.toml`; then module importers, settings fields against code and `.env.example`, declared dependencies against imports, and doc path references against the tree. Dynamic use was checked: FastAPI routes, LangGraph registration, the CLI entry point, composition wiring, mounted scripts and framework hooks.

| Item | Location | Evidence | Risk | Decision |
|---|---|---|---|---|
| `JiraStory` model | `src/ai_code_change_agent/application/jira/models.py:91-94` | Only the definition. No reference in src, tests, studio or docs; its last user `FetchJiraStory` was removed in `3293ea1` | Very low | **REMOVED** (Task 8 commit) |
| `domain/dependencies/manifest.py` references | `docs/DYNAMIC-DEPENDENCY-PROVISIONING.md:110,186` | The file does not exist; the validation lives in `domain/dependencies/policy.py` and `manifests.py` | None | **FIXED** (Task 8 commit) |
| `DirectoryBaseline` | `src/ai_code_change_agent/execution/baseline.py` | No production importer; used by `tests/unit/execution/test_paths_fs_policy.py:21` and `tests/integration/test_docker_workspace.py:31` as the fixture baseline | Moving it is an unrelated refactor | KEEP (test-support source) |
| `egress_proxy.py` | `src/ai_code_change_agent/execution/docker/egress_proxy.py` | No Python importer; bind-mounted and executed in the proxy container (`resolver_network.py:30,148-149`) | Removal breaks ADR-010 egress | KEEP (dynamic) |
| `attempt_tree`, `attempt_file` | `studio/live.py:137,145` | `@router.get` routes of the Studio API (`langgraph.json` `http.app`) | Studio UI | KEEP (route) |
| `make_planner`, `make_coder` | `studio/agent_graph.py:64,73` | Registered in `langgraph.json` `graphs` | Studio UI | KEEP (entry point) |
| `receive_jira_webhook`, `RequestIdMiddleware.dispatch` | `api/routes/jira_webhook.py:62`, `api/middleware/request_id.py:16` | FastAPI route and middleware hook | API | KEEP (framework) |
| `handle_starttag`/`handle_endtag`/`handle_data` | `integrations/web/fetcher.py:92-106` | `HTMLParser` callbacks | Fetcher | KEEP (framework) |
| `before_model`/`abefore_model`, `on_chat_model_start`, `on_llm_error` | `agents/coding/agent.py:103,107`, `application/costs/usage.py:199,245` | LangChain middleware and callback hooks | Cost guards | KEEP (framework, security) |
| `uvicorn` dependency | `pyproject.toml` | Not imported; used by `docker/api/Dockerfile` CMD and the CLAUDE.md/README run commands | API runtime | KEEP |
| `psycopg[pool]` extra | `pyproject.toml` | Not imported by src, but `langgraph/checkpoint/postgres/aio.py:25` imports `psycopg_pool` | Checkpointer import | KEEP |
| Undeclared direct imports `botocore`, `langchain_core`, `langchain` | `providers/llm/factory.py:10`, `agents/coding/*`, `application/costs/usage.py` | Transitive through `langchain-aws` and `deepagents`, locked in `uv.lock` | Low | KEEP (note only; declaring them is optional, not dead code) |
| Settings `app_host`, `app_port` | `core/config.py` | Consumed through `APP_HOST`/`APP_PORT` by the Dockerfile CMD and `compose.yaml` | Deployment | KEEP (kept in S2112 too) |
| `.env.example` | 76 names | Every name maps to a Settings field; no orphan | None | KEEP |
| `compose.yaml`, `docker/api`, `docker/execution`, `.dockerignore` | repository root, `docker/` | Postgres and API services, Python execution image only; no Node or Redis leftovers | None | KEEP |
| `scripts/verify_research_agent.py`, `studio/workspaces.py` `destroy_all()` | `scripts/`, `studio/` | Kept deliberately in S2112 (documented dev tools) | None | KEEP |
| Node.js mentions | `README.md:18,21`, `docs/ARCHITECTURE.md:17,20`, `docs/GETTING-STARTED.md:5`, `docs/MULTI-REPOSITORY.md:3`, `docs/PYTHON-EXECUTION-AND-RESEARCH.md:3` | Accurate: Node is only the Studio UI's dev requirement, and other languages are stated as out of scope | None | KEEP |
| `execution-node:0.1.0` mention | `docs/testing/E2E-CAMPAIGN-INITIAL-AUDIT.md:13` | A historical audit record (image already removed in S2112) | None | KEEP (record) |

REMOVE count: **1** (`JiraStory`), plus 1 stale doc reference. Both done in the Task 8 commit (`chore: remove the unused JiraStory model and fix stale S2113 doc references`); `rg -n "JiraStory" src tests` is now empty.

## 5. Hard-coded values audit (`src/`)

No repository, organisation, owner, Jira project or issue key is hard-coded. `SHOP` and `acme/shop-api` appear only as examples in CLI help (`cli/main.py:51-58`) and in one validation message (`core/config.py:208`).

| Value | Location | Class | Recommendation |
|---|---|---|---|
| `https://api.github.com` (3 copies), `https://github.com/` | `integrations/github/baseline.py:54`, `publisher.py:48,504`, `repositories.py:21`, `references.py:14` | Security restriction (fixed, "never from a Story, a model or the registry") | Keep. GitHub Enterprise is out of scope |
| `https://pypi.org/simple` | `core/config.py:139` (`DEPENDENCY_INDEX_URL`), defaults in `orchestration/steps.py:261`, `domain/dependencies/manifests.py:1426` | Operational setting (already externalised); the other two are fallback defaults that composition overrides (`composition.py:278-288,504`) | Keep |
| Model ids `claude-haiku-5-5` / `sonnet` / `opus` | `application/costs/pricing.py:101-108` | Registry value (built-in price table, overridable by `LLM_PRICING_FILE`) | Keep. The model used is `LLM_MODEL` |
| `web_search_20250305`, `max_uses: 1` | `integrations/web/search.py:37,99` | Invariant (API tool version and per-call bound) | Keep |
| Agent limits (80 model calls, $2/phase, recursion 250, 5400 s) | `agents/coding/configuration.py:14-23` | Operational settings (already externalised as `DEEP_AGENT_*`, `LLM_MAX_COST_PER_PHASE_USD`, `composition.py:486-487`) | Keep |
| Sandbox and container limits | `execution/docker/sandbox.py:60-70`, `runtime.py:72-82` | Security restriction with operational defaults (wired from Settings at `composition.py:256,369`) | Keep |
| `_DOCKER_FAILURE_CODES = {125, 126, 127}` | `runtime.py:58` | Invariant (Docker and coreutils exit semantics) | Keep; split 127 in Task 4 |
| Retry and backoff: `infra_retry_delays (2, 8)`, GitHub `RETRIES (0.5, 1.0)`, `MAX_BACKOFF_SECONDS 900`, backoff base 30 | `steps.py:263`, `github/baseline.py:60`, `jobs/worker.py:25-30` | Invariant (bounded retry policy) | Keep |
| `MAX_REPORT_ATTEMPTS 6`, `REPORT_STALE_SECONDS 300` | `multirepo/coordinator.py:45-46` | Invariant (report lifecycle) | Keep |
| Jira client pool (10/5), `retry_after_max_seconds 30`, reporter timeout 20 s | `integrations/jira/client.py:49,79`, `reporting.py:81` | Invariant | Keep |
| Size caps (`MAX_COMMENT 8000`, `MAX_BODY 60000`, ADF depth and nodes, snapshot, manifest and profile ceilings, `MAX_PATH_CHARS 300`) | `reporting.py:17`, `delivery/pull_request.py:14`, `jira/adf.py:12-13`, `git_providers/baseline.py:17-19`, `domain/repositories/catalog.py:11-16`, `domain/dependencies/manifests.py:39-43`, `workspaces/paths.py:10` | Security restriction or provider invariant | Keep |
| Protected paths | `application/workspaces/paths.py:21-22` | Security restriction | Keep (never configurable) |
| Prompt truncation (`feedback[:8000]`, `output_tail[-3000:]`) | `prompts.py:145`, `validation/schemas.py:133` | Invariant (context budget) | Keep |
| Branch names | `application/delivery/naming.py` (derived from issue key, attempt, changeset hash); the default branch comes from the registry | Invariant or registry value | Keep. No `main`/`master` literal in src |
| Commit author `noreply@ai-code-change-agent.invalid` | `integrations/github/publisher.py:49` | Invariant | Keep |

Recommendation: externalise nothing new. Everything that must vary per deployment is already a setting (`docs/CONFIGURATION.md`) or a registry value.

## 6. Observations (no change planned)

- O1: Studio (`studio/agent_graph.py:26-39`) builds the `CodingAgent` without callbacks, so Studio runs have no usage ledger and no phase USD guard (the model-call cap still applies). This is a development tool outside the worker path. Owner decision whether Studio should also attach a `UsageTracker`.
- O2: `_report_once` stores nothing for a *retryable* failure, so a later cleanup may post after a timeout that Jira actually accepted. Single-repository reporting has no `find_comment` reconciliation (the parent report does). This is existing behaviour, outside F5.
- O3: The PR re-read does not re-check `draft` (S2112 N8). This is still an owner decision.

## 7. Fix commits

| Task | Finding | Commit | Summary |
|---|---|---|---|
| 1 | Warning (section 3) | `238ae9d` | `fix(costs)`: the budget guard runs last, so a tripped guard leaves no unawaited callback |
| 2 | F2 | `3a5b3f9` | `test(costs)`: production chat models pinned as non-streaming; latent streamed-usage xfail removed |
| 3 | F1 | `cd86dbb` | `fix(agent)`: every untrusted block framed with a random per-block boundary id |
| 4 | F3 | `727662e` | `fix(validation)`: infrastructure failures and a missing executable (127) never spend the repair run |
| 5 | F4 | `dede242` | `fix(intake)`: permanent Jira errors fail at once; the job backoff honours `Retry-After` (capped at 900 s) |
| 6 | F5 | `d27771f` | `fix(reporting)`: a permanent single-repository Jira report failure is recorded once and never re-posted |
| 7 | F6 | `4c9298b` | `fix(multirepo)`: contract origin checked; integrity failures reported as not retryable |
| 8 | Dead code, stale docs | Task 8 commit (`chore: remove the unused JiraStory model and fix stale S2113 doc references`) | `JiraStory` removed; `docs/DYNAMIC-DEPENDENCY-PROVISIONING.md` points at `policy.py` and `manifests.py`; `docs/AUTOMATED-PIPELINE.md` documents the intake Jira error and `Retry-After` behaviour; this audit updated |

Observations O1 to O3 (section 6) remain owner decisions; no change was made for them.

## 8. Final verification (after Task 8)

| Gate | Result |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 329 files already formatted |
| `uv run mypy` (strict) | Success: no issues found in 296 source files |
| `uv run pytest -m "not docker"` (unit and PostgreSQL integration, with coverage) | **1511 passed, 22 deselected, 0 xfailed, 0 warnings**; coverage **91.20%** (threshold 90%) |
| `rg -n "JiraStory" src tests` | no match |

The 22 deselected tests are the `docker` marker tests. They were not run in this session, by instruction, because the Docker tests can clash with a live worker sandbox. Run `uv run pytest` (full suite) on an idle Docker host before merging to close this gap. Compared with the start (section 1: 1395 unit passed, 1 xfailed, 1 warning), the xfail and the warning are gone.
