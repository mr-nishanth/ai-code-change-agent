# Multi-repository Stories

One Jira Story can change several authorised **Python** repositories (for example a backend API plus a Python client, SDK or worker). Other languages (Node.js, React/Next.js, Android, Kotlin/Java, Flutter, iOS/Swift) are out of scope. Each repository is changed by its own **child execution**. A **parent execution** coordinates them deterministically; no model makes coordination or authorisation decisions.

## Story format

```text
Repositories:

- Repository: <owner>/backend-api
  Role: backend
  Responsibility: create tracking endpoints and response schemas
  Depends on: none

- Repository: <owner>/python-client
  Role: client
  Responsibility: add a typed client method for the tracking endpoint
  Depends on: backend-api

Dependencies:            (optional, same meaning as "Depends on")
- python-client depends on backend-api
```

- **Number of markers:**
  - one `Repository:` marker keeps the single-repository flow unchanged;
  - two or more create a parent;
  - at most `MULTI_REPO_MAX_REPOSITORIES` (default 5).
- **What markers can name:** a repository key, `owner/name` or a URL. They only *name* repositories. Every repository is resolved through the registry, which checks that it is ACTIVE, mapped to the Story's Jira project and has a published profile.
- **Dependencies:** they name declared repositories (key or repository name). They must form a DAG.
- **Nothing is hard-coded:** the parser reads only these markers and never names a repository, owner, role or project.

## Lifecycle

```
Story -> intake: parse markers -> resolve EVERY repository -> validate the dependency DAG
      -> parent (code_change_parents) + one child per repository (code_change_children)
      -> queue the children whose dependencies are complete (job code_change.child, dedupe key)
child: own attempt -> own workspace + sandbox (the Python execution image) -> own Deep Agent
       thread -> plan -> automatic PLAN gate -> implement -> independent validation
       -> draft PR (token scoped to that one repository) -> contract artifact (if published)
parent: when every child is final -> COMPLETED | PARTIALLY_COMPLETED | FAILED
        -> one aggregated Jira comment per report round, on the parent Story (durable lifecycle below)
```

- **Blocked before any work:** if any repository is unknown, DISABLED, unmapped or has no profile, or a dependency is unknown or cyclic, the parent is **BLOCKED** before anything runs (no clone, workspace, token or model call). One Jira comment then lists exactly what to fix. Repositories are never registered from Story text.
- **Ordering:** independent children run in parallel, bounded by `MAX_CONCURRENT_JOBS`. A dependent child is queued only after every upstream child has **COMPLETED** (validated, PR created) **and** published a contract.
- **Isolation:** each child has its own attempt, branch, workspace, sandbox, agent thread, validation and PR. Workspaces are never shared, and the GitHub token is scoped to that one repository and used only by worker delivery code.

## Cross-repository contracts

- **Publishing:** an upstream child whose repository has dependents is told to fill `published_contract` in its implementation report (endpoints, schemas, status codes and so on).
- **Storage:** after the child completes, the worker stores that text as an immutable `cross_repo_contract` artifact. The artifact is refused, never redacted, if it contains a secret. Its sha256 is recorded on the child.
- **Delivery to dependents:** dependent children receive the contract as **read-only context**. It is re-loaded for every phase run and used only when its hash, its schema **and** its origin match: the stored document must name this parent, the declared upstream repository and the upstream child's recorded attempt. Otherwise the dependent fails before any workspace or agent run with `artifact_hash_mismatch`, `artifact_schema_invalid`, `artifact_missing` or `artifact_provenance_mismatch`.
- **Integrity failures are not invited to retry:** for those codes the parent report says `Retryable: no (contract integrity failure: needs operator investigation before retry-child <id>)` instead of `yes`, because a blind retry would read the same artifact. The child stays FAILED and `retry-child` remains available once an operator has investigated. Storage errors (`artifact_too_large`, `artifact_storage_error`) and every other failure keep `Retryable: yes`.
- **No contract:** dependents are **BLOCKED** (`upstream_contract_missing:<repository>`) instead of guessing an API.

## Failures, retries and idempotency

- **Failed child:** its dependents are BLOCKED (`upstream_failed:<repository>`). Siblings that completed keep their PRs.
- **Retry:** `uv run ai-code-change changes retry-child <child-id>` re-runs only that child, plus the children it blocked. It starts a new attempt and branch; history is kept. The parent then reports again in a new round.
- **Replays:** a replayed webhook or intake reuses the existing parent. Every status change is compare-and-set, and every child job has a dedupe key.
- **Worker crash:** the job is reclaimed when its lease expires. The child resumes its own attempt, and a stale workspace from the dead run is removed first.

## Jira reporting (parent Story)

The aggregated result is posted to the **parent Story** (the issue the webhook came from), never to another issue. It lists every repository, its status, branch and draft PR (or `not recorded`), failure reason and `retry-child` command, published contracts, runs, the total LLM cost, the completion time and a **Report ID** (`aica-report-<parent-id>-r<round>`).

- **Execution status vs reporting status:** `code_change_parents.status` says what the code change did; `jira_report` says whether Jira has the comment. A reporting failure never changes the execution status and never re-runs a child or creates a PR.
- **Lifecycle** (`jira_report`): `NULL` (pending) -> `IN_PROGRESS` -> `SUCCEEDED` | `RETRYABLE_FAILURE` | `PERMANENT_FAILURE` (`DISABLED` when `JIRA_REPORTING_ENABLED=false`). The Jira comment id, the attempt count, the last error class and the time are stored with it.
- **Classification:** 201 = success (comment id stored). 400/401/403/404 and an invalid issue key are permanent. 429, 5xx, timeouts and network errors are retryable.
- **Idempotency:** the POST is never retried blindly. Before any second POST of a round (after a timeout, a 429/5xx, or a claim left `IN_PROGRESS` by a crashed worker for more than 5 minutes), the worker reads the issue's comments and looks for the round's Report ID. Found: the existing comment id is stored and nothing is posted. Lookup failed: nothing is posted, and the report stays retryable.
- **Retries:** a retryable outcome enqueues one `code_change.parent_report` job per round in the PostgreSQL job queue, which retries it with backoff. After 6 attempts the report becomes `PERMANENT_FAILURE` (error `retries_exhausted:...`).
- **Rounds:** `retry-child` opens a new round, so earlier comments of the Story stay as history and the new outcome gets one new comment.
- **Limits:** the lookup reads the newest 100 comments of the issue. Comments posted before this lifecycle existed have no Report ID; KAN-9's final comment (10009) was reconciled by hand.

## Verified (2026-11-26, KAN-9, claude-haiku-5-5)

| Repository | Role | Result | Draft PR |
|---|---|---|---|
| autoSoft_connector | backend (FastAPI tracking endpoint) | COMPLETED, contract sha256 `314bf81684b6` | https://github.com/mr-nishanth/autoSoft_connector/pull/1 |
| cdk_connector | client (httpx client, depends on backend) | COMPLETED; calls exactly the published `GET /api/v1/orders/{order_id}/tracking` | https://github.com/mr-nishanth/cdk_connector/pull/1 |

What the run exercised:
- dependency ordering: the client was queued only after the backend completed;
- contract hand-off;
- partial failure plus `retry-child`, used twice with siblings kept;
- one aggregated Jira comment per round (final: COMPLETED with both PRs);
- per-repository cost: $0.4261 backend, $0.0089 client.

The backend cost includes a planning loop (176 structured-output retries) that is now prevented; see LLM-COST-TRACKING.md, "Runaway-cost guards".
'

## Verified live run: KAN-10 (2026-11-26)

Story "Shared vehicle lookup contract across three connectors", created in Jira project KAN at 12:47:31 UTC; parent `baddad59-2ce6-4019-ab9d-e27ebbf19b32` COMPLETED at 12:51:49 UTC (about 4.5 minutes). Haiku (`claude-haiku-5-5`), automatic approval mode, `$2` per phase.

| Repository | Role | Attempt | Order (worker time) | Tests (offline) | Contract sha256 | Draft PR |
|---|---|---|---|---|---|---|
| reynolds_connector | backend | `111a8a7e` | 18:17:39 to 18:18:57 | 16 passed | `a66b3aea7e26...` | reynolds_connector#2 |
| automate_connector | client | `a2d47163` | 18:19:04 to 18:20:38 | 22 passed | `d17cd99962e8...` | automate_connector#3 |
| autoSoft_connector | worker | `2aa6eb60` | 18:20:44 to 18:21:50 | 18 passed | (no dependents) | autoSoft_connector#2 |

- **Order:** each child started only after its upstream job succeeded, and each downstream plan used the upstream contract (the client'\''s plan quotes the backend'\''s VehicleRecord contract; the worker'\''s the client'\''s interface). The contract artifacts'\'' content hashes equal the hashes stored on the children.
- **Dependencies:** the client'\''s plan requested `httpx`, `pytest` and `tenacity`. A bundle (key `18d20a90...`, 13 wheels including `tenacity==9.2.1`) was resolved through the internal resolver network and egress proxy, and every wheel'\''s sha256 matched PyPI'\''s published PEP 691 hash (9 of 13 have a PEP 740 attestation published, recorded but not verified). The bundle is read-only and validation reused it (cache hit).
- **GitHub (read back through the API):** three open **draft** PRs on new `aica/kan-10-*` branches against `master`, one commit each, no auto-merge, exactly one KAN-10 PR per repository.
- **Jira (read back through the API):** exactly one comment (`10010`) with three repository results, three PR links, Validation PASSED, the two published contract hashes, total LLM cost $0.0265; `jira_report=SUCCEEDED` and `jira_comment_id=10010` persisted on the parent.
- **Cost:** reynolds $0.0099, automate $0.0100, autosoft $0.0067 (36 model calls in total). The research tools were registered but the agents did not call them in this run (the Story needed no external documentation); agent-driven use is shown separately in ADR-011.
'
