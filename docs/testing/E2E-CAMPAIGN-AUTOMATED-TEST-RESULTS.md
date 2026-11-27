# E2E campaign: automated test results (S2112)

Branch `feature/s2112-edge-case-tests`, commits `70ef6c6..3293ea1`. This milestone adds mocked and isolated automated tests, a small number of production fixes, configuration changes and a dead-code cleanup. **Live end-to-end verification against Jira, GitHub or a model provider was NOT TESTED in this milestone.** Mocked results below do not count as live results.

Sources: [E2E-CAMPAIGN-INITIAL-AUDIT.md](E2E-CAMPAIGN-INITIAL-AUDIT.md) (section 5 lists the gaps this milestone addresses), the per-task implementation reports, the whole-branch review, and `git log 70ef6c6..HEAD`.

## 1. Baseline

| Item | Value |
|---|---|
| Commit | `9df4fca` (S2111) |
| pytest | 1,338 passed, 0 failed, 0 skipped (PostgreSQL and Docker tests included) |
| Coverage | 90.51% (gate 90%) |
| ruff, ruff format, strict mypy | clean (296 files) |

## 2. Commits

| Commit | Scope |
|---|---|
| `5841867` | test(multirepo): contract integrity, chains, failures, resumption (plus 2 coordinator fixes) |
| `6d06802` | test(costs): provider failures, retry bounds, ledger edge cases |
| `89302d3` | test(validation): command timeouts, missing executables, crashes, publishing boundary |
| `ef7dc4d` | test(github): publisher write-path rate limits, timeouts, reconciliation, rejections |
| `7f7a941` | test(jira): intake outages, interrupted webhook persistence, lease reclaim, permanent report failures |
| `6d1c5d5` | test(references): SSRF address classes, mixed DNS answers, redirect loops, injection, relative links (plus search query fix) |
| `dc874c2` | test: address review findings (NAT64 and related address hardening, shared static URL check, test fixes) |
| `5765a8c` | feat(research): reference limits and ranking hosts configurable |
| `3293ea1` | chore: remove unused code and out-of-scope references |

## 3. Scenarios added, by area

Unless a row says otherwise, every test passes. "Expected" is what the test asserts, and the actual result matched it. Parametrized cases are grouped as `[a, b]` or by case count.

### 3.1 Multi-repository orchestration

Production module: `src/ai_code_change_agent/application/multirepo/coordinator.py`. Test file: `tests/unit/multirepo/test_multirepo.py`.

| Test | Expected (= actual) |
|---|---|
| `test_a_contract_whose_content_no_longer_matches_its_hash_is_refused[recorded_hash, stored_bytes]` | `ArtifactIntegrityError(artifact_hash_mismatch)` |
| `test_a_tampered_contract_fails_the_dependent_before_any_agent_or_workspace` | Dependent FAILED/`artifact_hash_mismatch` at planning; no sandbox or agent; parent posts one PARTIALLY_COMPLETED report |
| `test_a_contract_with_an_invalid_structure_is_refused_before_use[not-json, missing-fields, empty-contract, unknown-field]` | `artifact_schema_invalid` before the dependent sees it |
| `test_an_unusable_published_contract_is_never_recorded_and_blocks_dependents[absent, blank, not-text]` | No contract recorded; dependent BLOCKED `upstream_contract_missing`, never queued |
| `test_a_contract_produced_by_another_child_is_refused` | Swapped ref/hash from another child refused (`artifact_missing`) |
| `test_a_published_contract_reaches_only_the_children_that_depend_on_it` | Dependent gets exactly the upstream contract; independent sibling gets none |
| `test_a_dependent_whose_upstream_has_no_recorded_contract_cannot_start` | `MultiRepoBlockedError(upstream_contract_missing)` |
| `test_a_three_level_chain_runs_strictly_in_dependency_order` | A, then B, then C; replayed advance starts nothing early; one report in order |
| `test_a_missing_contract_at_the_top_of_a_chain_blocks_every_level_below` | B BLOCKED `upstream_contract_missing`, C BLOCKED `upstream_failed:B` in one pass; parent reports. **Failed before fix (defect D1)** |
| `test_a_failure_at_the_top_of_a_chain_blocks_every_level_below_in_one_pass[FAILED, REJECTED]` | Cascade through the `upstream_failed` branch; only the top child queued; parent FAILED, one report (added in the review fix wave; mutation-checked) |
| `test_an_upstream_that_fails_blocks_dependents_and_the_independent_sibling_still_runs[FAILED, REJECTED]` | Dependent BLOCKED, runs=0; sibling completes; parent PARTIALLY_COMPLETED |
| `test_a_duplicate_dependency_edge_is_deduplicated` | Edge stored once; contract in agent context once. **Failed before fix (defect D2)** |
| `test_several_failures_and_a_block_are_each_reported_in_one_comment` | One comment listing each child, 3 retry hints, "no repository was delivered"; replay posts nothing |
| `test_a_resumed_parent_never_reruns_completed_children` | Only the pending child gets a new attempt; unrecorded finished attempt is recorded, not re-run; second replay changes nothing |

Already covered (not duplicated): cycles and unknown nodes, too many repositories, undeclared dependency, unauthorised repository, upstream-first fan-out with one report, missing contract in the fake store, partial failure with retry of the failed child only, partial-success report, replayed intake, Jira crash/retry/duplicate reports.

### 3.2 Contracts

Covered by the contract rows in 3.1 (hash mismatch, invalid structure, unusable published contract, contract from another child, scoping to dependents). Production module: `coordinator.py` (`context_for`, `_publish_contract`). See the open items in section 5 (provenance not checked on read; hash mismatch gives FAILED).

### 3.3 LLM provider failures and cost accounting

Production modules: LLM provider construction (`build_chat_model`), the coding agent, `application/costs/usage.py` (UsageCallbackHandler, UsageTracker), BudgetGuard. Providers are faked without network: Anthropic through an `httpx` MockTransport behind the real SDK client and retry policy (`tests/support/models.py::anthropic_over_fake_http`); Bedrock through a botocore before-send hook.

| Test | Expected (= actual) |
|---|---|
| `tests/unit/test_llm_provider.py::test_anthropic_retries_rate_limits_within_the_policy` | 429: 1 request + LLM_MAX_RETRIES (2) retries, then stop |
| `tests/unit/test_llm_provider.py::test_bedrock_retries_throttling_within_the_policy_and_never_retries_access_denied[429-ThrottlingException-3, 403-AccessDeniedException-1]` | Throttling bounded at retries + 1; 403 not retried |
| `tests/unit/agents/test_coding_agent.py::test_provider_http_failures_end_the_run_with_a_fixed_non_secret_code[429, 401, 403]` | Fixed codes (`agent_error_anthropicratelimiterror`, `...authenticationerror`, `...permissiondeniederror`); key and provider message absent; 401/403 one request; one failed ledger row, cost unknown |
| `tests/unit/agents/test_coding_agent.py::test_a_malformed_provider_response_is_a_typed_failure` (2 cases) | Non-JSON or wrong-shape 200 gives a typed `agent_error_*` (no body text), not retried |
| `tests/unit/agents/test_coding_agent.py::test_a_tool_that_raises_fails_the_run_with_a_fixed_code_and_no_internals` | `agent_error_runtimeerror`; message not leaked; model not called again |
| `tests/unit/costs/test_costs.py::test_a_failed_call_without_streamed_usage_is_unknown_and_adds_nothing_known` | Recorded once, usage/cost None (unknown, not zero) |
| `tests/unit/costs/test_costs.py::test_usage_streamed_before_a_failure_is_recorded_and_charged` | **xfail (strict)**: defect N1, not fixed |
| `tests/unit/agents/test_coding_agent.py::test_the_cost_guard_stops_the_run_even_while_output_retries_remain` | `agent_cost_budget_exceeded` after 1 call although retries remain; searches refused |
| `tests/unit/costs/test_costs.py::test_a_replayed_accounting_event_is_handed_to_the_ledger_which_owns_the_dedupe` | Both records reach the store with the same provider message id; dedupe is the database's (proved in `tests/integration/test_usage_ledger_db.py`). Rewritten after review: the original version only tested the fake store |
| `tests/unit/costs/test_costs.py::test_an_error_after_usage_was_recorded_keeps_the_recorded_usage` | Known cost kept; totals INCOMPLETE |
| `tests/unit/agents/test_coding_agent.py::test_a_provider_error_after_a_priced_call_keeps_the_recorded_usage` | Success row keeps usage/cost; failed row unknown; guard spend unchanged |
| `tests/unit/costs/test_costs.py::test_an_unpriced_model_is_recorded_as_unknown_cost_and_flagged_in_reports` | Tokens kept, cost None, reports flag "unknown, not zero" |

Already covered: TimeoutError mapping, failed calls with unknown cost, structured-output retries and model-call cap, phase budget at guard level, unpriced model never free, searches charged to the guard, handler-level duplicate suppression, DB-level ledger idempotency (append-only trigger).

### 3.4 Validation and the publishing boundary

Production modules: `validation/` engine and `test_runner`, `execution/docker/runtime.py` (`DockerWorkspaceRuntime._execute`), `orchestration/steps.py`.

| Test | Expected (= actual) |
|---|---|
| `tests/unit/validation/test_rules_and_decision.py::test_each_validation_command_failing_or_not_running_fails_closed` (12 cases: {fails, times_out, exit_127} x {test, lint, format, types}) | Decision FAILED; command FAILED / TIMED_OUT / ERROR; REQUIRED_TESTS fails only for test; workspace destroyed |
| `tests/unit/validation/test_rules_and_decision.py::test_a_docker_crash_while_running_tests_fails_rebuild_integrity` | REBUILD_INTEGRITY FAILED `docker_unavailable` |
| `tests/unit/validation/test_rules_and_decision.py::test_an_unexpected_runtime_error_propagates_and_still_destroys_the_workspace` | Error propagates; workspace destroyed |
| `tests/unit/validation/test_rules_and_decision.py::test_the_engine_refuses_a_profile_without_a_test_command` | REQUIRED_TESTS fails |
| `tests/unit/orchestration/test_automatic_workflow.py::test_no_validation_failure_mode_reaches_the_publisher` (11 cases: timeout, test/ruff/format/mypy failure, lint timeout, executable missing, docker crash, runtime error, secret detected, protected file modified) | Attempt FAILED with the expected code; publisher never called; no delivery manifest; failure reported once |

No publishing-boundary defect found: every failure mode fails closed. Already covered: clean change passes, failing test output, static rules, secret detection, unapplicable change, default test command fallback, single repair with feedback, failed validation never publishes.

### 3.5 GitHub publishing

Production module: `src/ai_code_change_agent/integrations/github/` publisher. Test file: `tests/unit/github/test_publisher_unit.py` (FakeGitHub over `httpx.MockTransport`, real git objects). Mutation checks confirmed the PR lookup, rate-limit check and ref read are each needed.

| Test | Expected (= actual) |
|---|---|
| `test_a_rate_limited_ref_creation_is_a_typed_failure_and_writes_no_branch[403-remaining-0, 429-retry-after]` | DELIVERY_FAILED `provider_rate_limited`; no branch; later delivery creates one branch and PR |
| `test_a_rate_limited_pull_request_creation_reports_the_branch_and_no_pull_request[403-remaining-0, 429-retry-after]` | Branch published, no PR claimed; retry creates one PR, reuses the ref |
| `test_a_timed_out_ref_creation_is_unknown_and_the_retry_looks_before_creating[before, after]` | DELIVERY_OUTCOME_UNKNOWN; retry reads the ref first; reused or created exactly once |
| `test_a_pull_request_created_behind_a_timeout_is_adopted_not_duplicated` | Retry adopts the existing PR; one POST /pulls overall. (Draft flag is read from the fake; the publisher does not re-check draft, see N8) |
| `test_a_repeated_delivery_reuses_the_branch_and_pull_request` (existing, extended) | No non-GET call; ref and PR steps REUSED |
| `test_a_token_expired_mid_publish_is_refreshed_once_and_the_write_retried` | One token refresh; write retried; PR created |
| `test_a_write_still_unauthorised_after_one_refresh_is_a_typed_auth_failure` | `installation_auth_failed`; one refresh only |
| `test_a_repository_that_cannot_be_found_is_refused_before_any_write[404, 403]` | `repository_inaccessible`; no writes |
| `test_a_pull_request_rejected_by_validation_is_not_claimed` | 422: branch published, `pull_request_rejected`, no PR number |

No production defect found. Already covered: verified draft PR on the pinned base, concurrent ref reuse, 5xx as unknown outcome, forbidden write, repository removed from installation, preflight refusal, unreadable PR not claimed, token scoping/refresh, read-path rate limits.

### 3.6 Jira intake and reporting

Production modules: intake handler, `JiraOutcomeReporter`, webhook repository, `orchestration/steps.py` (`_report_once`). Jira is faked (FakeJira or MockTransport behind the real reporter); PostgreSQL tests use the compose service.

| Test | Expected (= actual) |
|---|---|
| `tests/integration/test_intake_jira_outage_db.py::test_jira_unavailable_during_intake_is_retried_and_then_starts_exactly_one_attempt[timeout, connection, http_503, http_429]` | Job back to QUEUED, request untouched, 0 attempts; retry after backoff starts exactly one attempt |
| `tests/integration/test_intake_jira_outage_db.py::test_jira_unavailable_for_every_attempt_ends_dead_with_the_request_untouched` | DEAD after 3 attempts; request untouched |
| `tests/integration/test_webhook_repository.py::test_an_interruption_after_the_delivery_insert_leaves_nothing_and_redelivery_is_once[request_insert, delivery_link_update]` | Statement N raises: full rollback; redelivery ACCEPTED once; later delivery DUPLICATE; one request, one job |
| `tests/unit/orchestration/test_automatic_workflow.py::test_a_permanent_jira_comment_failure_is_not_retried_or_marked_reported` (6 cases: {completed, failed} x {400, 401, 403}) | One POST, not retried; outcome unchanged; not marked reported |
| `tests/unit/multirepo/test_multirepo.py::test_auth_and_permission_errors_are_permanent_and_not_retried[400]` (400 added) | Parent PERMANENT_FAILURE `http_400`; no report job |
| `tests/unit/jira/test_story_and_reporting.py::test_comment_post_failures_are_classified[400]` (400 added, single-request assertion for all statuses) | PERMANENT `http_400`; one request each |

Multi-repo parent report with several failed children: see `test_several_failures_and_a_block_are_each_reported_in_one_comment` (3.1). No production defect found. Mutation checks confirmed the report guard and intake retry classification are each needed.

### 3.7 Recovery and runtime

| Test | Production module | Expected (= actual) |
|---|---|---|
| `tests/integration/test_jobs_and_changes_db.py::test_an_expired_lease_is_reclaimed_by_one_worker_and_a_second_expiry_is_dead` | `PostgresJobQueue` | 8 racing claimers, exactly one wins (attempts=2); second expiry DEAD `lease_expired_attempts_exhausted`; late `complete()` returns False |
| `tests/unit/execution/test_docker_runtime_unit.py::test_a_container_that_dies_unexpectedly_is_a_failed_command_never_a_pass[sigkill_oom, sigsegv, sigterm, no_status]` | `execution/docker/runtime.py` | FAILED (never PASSED), exit code kept, container removed |
| `test_a_resumed_parent_never_reruns_completed_children` (3.1) | coordinator, ChildHandler | No completed child re-run after interruption |

Already covered: sequential lease reclaim, reaping with no attempts left, retry backoff then DEAD, real SIGKILLed worker reclaimed once, runtime exit-code mapping (1/5/125/127/timeout), Docker unavailable is an error. Not covered: API restart mid-request (audit gap 6) as a live scenario; only the webhook transaction rollback is tested.

### 3.8 Documentation research security

Production modules: `src/ai_code_change_agent/integrations/web/fetcher.py`, `integrations/web/search.py`, `core/reference_hosts.py`, `agents/coding/tools.py`.

| Test | Expected (= actual) |
|---|---|
| `tests/unit/test_reference_fetcher.py::test_link_local_unique_local_and_mapped_resolutions_are_refused_without_a_request` (9 cases: 169.254.1.1, 100.64.0.1, 0.0.0.0, fd00::1, fc00::5, fe80::1%en0, ::ffff:127.0.0.1, ::ffff:10.0.0.1, ::) | Refused, no request |
| `tests/unit/test_reference_fetcher.py::test_a_host_resolving_to_public_and_private_addresses_is_refused` (3 cases) | Mixed DNS answer refused on direct fetch and redirect hop |
| `tests/unit/test_reference_fetcher.py::test_ipv6_forms_that_embed_an_ipv4_address_are_checked_by_what_they_reach` (14 cases) | Allowed: 64:ff9b::808:808, 2606:4700::1111, 104.16.0.1. Refused: NAT64 to loopback/metadata/private/multicast, ::/96, ::ffff:0:0:0/96, fec0::/10, 6to4. **Failed before fix (D4)** |
| `tests/unit/test_reference_fetcher.py::test_an_a_to_b_to_a_redirect_loop_stops_at_the_redirect_limit` | `reference_too_many_redirects` after 4 requests |
| `tests/unit/test_reference_fetcher.py::test_prompt_injection_in_a_document_stays_inside_the_untrusted_markers` | Injected text stays between the markers; real closing marker is the last line (marker spoofing not addressed, see N2) |
| `tests/unit/test_reference_search.py::test_search_hits_with_a_query_string_or_an_unlisted_host_are_not_returned` | Query-string and unlisted hits dropped; fragments kept. **Failed before fix (D3)** |
| `tests/unit/test_reference_search.py::test_search_never_offers_a_url_that_fetch_reference_would_refuse` | Drops non-443 port, userinfo, over-length URL, token in path, non-numeric port; keeps :443 and upper-case host |
| `tests/unit/test_reference_hosts.py::test_relative_llms_txt_links_are_fetched_as_absolute_urls_on_the_same_host` | Relative path resolved on the same host; protocol-relative off-host link refused |

Already covered: metadata/loopback/private resolutions, every redirect hop checked, self-loop redirect limit, untrusted markers and fetch budget, unlisted subdomains and http hits, absolute llms.txt links, agent URL query/length/secret refusal.

## 4. Production defects found and fixed

| ID | Description | Root cause | Fix | Test | Risk |
|---|---|---|---|---|---|
| D1 | Coordinator chain block: in A->B->C, when A fails or completes without a contract, B became BLOCKED but C stayed WAITING; the parent hung RUNNING with no Jira report | `Coordinator.advance` used a snapshot of the children taken before the loop, so C still saw B as WAITING | After a successful compare-and-set to BLOCKED, update the snapshot entry (both `upstream_failed` and `upstream_contract_missing` branches) (`5841867`) | `test_a_missing_contract_at_the_top_of_a_chain_blocks_every_level_below`, `test_a_failure_at_the_top_of_a_chain_blocks_every_level_below_in_one_pass[FAILED, REJECTED]` | Low: affects only children that would otherwise be stuck; retry still reopens `upstream_*` blocks |
| D2 | Duplicate dependency edges (repeated name or `owner/name` alias) were stored; the agent received the same contract several times | The parser removes duplicates only across sections, and alias resolution can merge spellings | `Coordinator.start` records each resolved upstream key once (`5841867`) | `test_a_duplicate_dependency_edge_is_deduplicated` | Very low; already-persisted duplicate rows still work but keep duplicate contracts |
| D3 | Search query/static URL filter: `search_docs` offered hits that `fetch_reference` always refuses (query string, non-443 port, userinfo, over-length, secret in URL), each wasting a fetch | Hit filter checked only scheme, exact host and duplicates, and split the host by hand | First a query check (`6d1c5d5`); then one shared predicate `url_refusal` (now in `core/reference_hosts.py`) used by both fetcher and search, with a required `contains_secret` argument (`dc874c2`, `5765a8c`) | `test_search_hits_with_a_query_string_or_an_unlisted_host_are_not_returned`, `test_search_never_offers_a_url_that_fetch_reference_would_refuse` | Low: only narrows the hits; fetcher behaviour unchanged |
| D4 | Address hardening: NAT64 `64:ff9b::/96` to private targets, IPv4-compatible `::/96`, SIIT `::ffff:0:0:0/96` and site-local `fec0::/10` were treated as public (stdlib `is_global=True` on Python 3.12.13) | The check relied only on `ip.is_global` | New `address_is_public(ip)` in `fetcher.py`: base rule unchanged; `::/96`, `::ffff:0:0:0/96`, `fec0::/10` refused by explicit list; NAT64 allowed only when the embedded IPv4 is global and not multicast (`dc874c2`) | `test_ipv6_forms_that_embed_an_ipv4_address_are_checked_by_what_they_reach` (14 cases) | Low: only refuses more; NAT64 to public IPv4 still allowed for IPv6-only DNS64 networks. DNS re-resolution at connect (TOCTOU) remains an accepted risk |

## 5. Defects and gaps found but NOT fixed

| ID | Finding | Reason not fixed | Suggested follow-up |
|---|---|---|---|
| N1 | Usage streamed before a provider error is dropped: not in the ledger, not charged to the phase budget | Latent (the agent does not stream). Fixing it changes the documented failed-call policy in `docs/LLM-COST-TRACKING.md`. Pinned by strict xfail `tests/unit/costs/test_costs.py::test_usage_streamed_before_a_failure_is_recorded_and_charged` | Read usage from `on_llm_error`'s `response` in both handlers; do it with any change that enables streaming |
| N2 | Untrusted-marker spoofing: document text (and Story text, contract text, validation output, search titles in `agents/coding/prompts.py` and `tools.py`) can contain a fake "(UNTRUSTED DATA ENDS)" line; `content_type` in the header is server-controlled | Deferred for owner approval: the fix changes the prompt/tool output format and security policy | Per-call random boundary ids, one `frame_untrusted` helper for all five places, collapse newlines in titles, restrict `content_type` |
| N3 | Infrastructure failures during validation (Docker crash, exit 125/126, timeouts) are classified `validation_failed` and consume the repair run; exit 127 shares `container_start_failed` | Policy decision (`INFRA_CODES`). Safe: nothing is published | Non-repairable `infrastructure_failed` code; distinct `command_not_found` for 127 |
| N4 | Intake retries Jira 401/403/invalid responses up to 3 times; only 404 is permanent | Small policy change; bounded and safe | Map `JiraAuthenticationError`, `JiraPermissionError`, `JiraResponseError` to `JobFailedError` |
| N5 | Single-repository permanent Jira comment failure (400/401/403) is only logged, not persisted; a second `cleanup()` for the same attempt could post again | Needs a new artifact or state (design change). Multi-repo parent already records PERMANENT_FAILURE | Record a `JIRA_REPORT` with PERMANENT_FAILURE |
| N6 | Contract provenance not checked on read: `context_for` checks hash and schema, not `repository_key`/`attempt_id`/`parent_id` | Defence in depth: artifact scope already ties a contract to its producer (cross-child swap is refused); exploiting it needs artifact write access | Compare provenance fields; raise `artifact_provenance_mismatch` |
| N7 | Contract hash mismatch makes the dependent FAILED (not BLOCKED), and the parent report says "Retryable: yes", but retrying fails again | Existing design; fails closed, no agent or workspace runs | Print "Retryable: no (integrity failure, operator action)" for `artifact_*` codes |
| N8 | PR re-read (`_matches`) does not assert `draft`; a non-draft PR on the head branch would be adopted | Not enforced on reuse because a human may mark the PR ready; test claim was reworded | Owner decision whether newly created PRs must be re-verified as drafts |

Other observations (no change): provider and intake error codes derive from exception class names (pinned by tests); BudgetGuard does not dedupe repeated end events (conservative); unpriced-model wording in totals says "without usage"; a pre-existing "coroutine never awaited" RuntimeWarning; `anthropic_over_fake_http` patches a private LangChain function and should also use an unroutable base URL; 8 minor test-hygiene review items were left (duplicated helpers, private rig attribute access).

## 6. Configuration made dynamic

| Setting | Default | Bounds | Used by |
|---|---|---|---|
| `REFERENCE_MAX_URL_CHARS` | 300 | 50-2000 | fetcher, search, `fetch_reference` tool description |
| `REFERENCE_MAX_REDIRECTS` | 3 | 0-10 | fetcher |
| `REFERENCE_SEARCH_MAX_HITS` | 8 | 1-20 | search |
| `secondary_hosts` in `config/reference_sources.yaml` | `[pypi.org, raw.githubusercontent.com]` | optional; each must be in `allowed_hosts`; exact match | search ranking (demoted hosts) |

Defaults equal the previous hard-coded values. Wired in `composition.build_reference_tools`; documented in `.env.example`, `docs/CONFIGURATION.md`, `docs/PYTHON-EXECUTION-AND-RESEARCH.md`, ADR-011 and `docs/SECURITY.md`. `npmjs.com` and `github.com` were dropped from ranking (not trusted hosts). `LLM_PRICING_FILE` was kept. The audit found no hard-coded repositories, projects or keys in `src/`. Tests: `tests/unit/test_settings.py` (defaults, 6 bound cases, env names), `tests/unit/test_reference_hosts.py` (secondary hosts valid/invalid, 6 invalid cases), `tests/unit/test_reference_search.py` (ranking, hit cap, URL length), `tests/unit/test_reference_fetcher.py` (redirect limit 0/1/5, URL cap), `tests/unit/orchestration/test_intake_and_composition.py` (wiring).

## 7. Removed as unused (`3293ea1`)

| Item | Note |
|---|---|
| `orchestration/steps.py` `Sandbox` protocol | unreferenced |
| `application/jira/services.py` `FetchJiraStory` | module and `tests/unit/jira/test_services.py` (4 tests) deleted |
| `domain/changes/errors.py` `ReprocessNotAllowedError`, `NotAuthorisedError` | never raised |
| `application/delivery/errors.py` `INTEGRITY_CODES`, `CONFLICT_CODES` | unreferenced |
| `application/git_providers/delivery.py` `FAILURE_OUTCOMES` | unreferenced |
| `multirepo/coordinator.py` `child_statuses()`, `TERMINAL_CHILD` | unreferenced |
| `artifacts/service.py` `new_scope()` | unreferenced |
| `github/authentication.py` `REQUIRED_PERMISSIONS` | unreferenced |
| `dependencies/manifests.py` `_SELF_EXTRA_RE` | unreferenced |
| `repositories/catalog.py` `EXECUTION_TIERS` | unreferenced; tier still validated by a Literal |
| `core/reference_hosts.py` `load_allowed_hosts()` | tests use `load_reference_sources` |
| `docs/GETTING-STARTED.md` `--toolchain node` hint | no Node toolchain exists |
| Local image `ai-code-change-agent/execution-node:0.1.0` | removed from the host (not in git) |

Doc edits: ADR-010 approval section marked as given; `docker/execution/requirements.in` comment "proposed" changed to "implemented". Kept deliberately: `studio/` and `deep-agent-ui/` (documented dev tool, including the unreferenced `studio/workspaces.py` `destroy_all()`), `PRICING_SOURCE`, `sweep()`, `check_ready()`, `waiting_for()`, `llm_configured`, `html_to_text()`, `app_host`/`app_port`, `workspace_network_policy`, the profile schema vocabulary for BUILD/INSTALL/network, `scripts/verify_research_agent.py`, and the negative non-Python tests.

## 8. Test commands

```bash
uv run pytest                      # full suite with coverage gate (90%)
uv run pytest -m integration       # needs PostgreSQL (compose service on 127.0.0.1:5435)
uv run pytest -m docker            # needs Docker and the local execution image
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## 9. Final counts

FINAL COUNTS (controller run on this commit's tree): 1483 passed, 1 xfailed, 1 warning in 91.18s (0:01:31); skipped 0; coverage 91.69% (gate 90%); ruff, ruff format and strict mypy (297+ files) clean. Baseline at 9df4fca: 1,338 passed, 90.51%. Docker and PostgreSQL integration tests included.

## 10. Remaining gaps needing live verification

All live E2E is **NOT TESTED** in this milestone. Every result above uses fakes (MockTransport, FakeJira, FakeGitHub, fake Docker runner) or local PostgreSQL. None of the following has been observed against the real services:

| Area | Needs live verification |
|---|---|
| Jira | Real webhook delivery and signature, Story intake, intake behaviour during a real Jira outage or credential failure, single- and multi-repo outcome comments (KAN-12 to KAN-15 not created) |
| GitHub | Real App token exchange and refresh, branch and draft PR creation, behaviour under real rate limits and timeouts, PR adoption after a timed-out create |
| Model provider | Real Anthropic or Bedrock retries, 429/401/403 handling, usage and cost recorded in the ledger against real billing, phase budget stop |
| Multi-repository | Real three-repository run with contract hand-off (KAN-13 candidate needs owner confirmation) |
| Validation and sandbox | Real Docker crashes, OOM kills and timeouts in the execution image |
| Documentation research | Live fetches and search against trusted hosts; `pydantic.dev` still not approved; NAT64 behaviour on a real IPv6-only network |
| Recovery | API restart mid-request, real worker crash during a live run |
