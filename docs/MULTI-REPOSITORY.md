# Multi-repository Stories (S2045)

One Jira Story can change several authorised repositories (backend, web, mobile, libraries). Each repository is changed by its own **child execution**. A **parent execution** coordinates them deterministically; no model makes coordination or authorisation decisions.

## Story format

```text
Repositories:

- Repository: <owner>/backend-api
  Role: backend
  Responsibility: create tracking endpoints and response schemas
  Depends on: none

- Repository: <owner>/web-app
  Role: frontend
  Responsibility: implement the order tracking interface
  Depends on: backend-api

Dependencies:            (optional, same meaning as "Depends on")
- mobile-app depends on backend-api
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
child: own attempt -> own workspace + sandbox (its profile's toolchain image) -> own Deep Agent
       thread -> plan -> automatic PLAN gate -> implement -> independent validation
       -> draft PR (token scoped to that one repository) -> contract artifact (if published)
parent: when every child is final -> COMPLETED | PARTIALLY_COMPLETED | FAILED
        -> one aggregated Jira comment per report round (claimed in the database)
```

- **Blocked before any work:** if any repository is unknown, DISABLED, unmapped or has no profile, or a dependency is unknown or cyclic, the parent is **BLOCKED** before anything runs (no clone, workspace, token or model call). One Jira comment then lists exactly what to fix. Repositories are never registered from Story text.
- **Ordering:** independent children run in parallel, bounded by `MAX_CONCURRENT_JOBS`. A dependent child is queued only after every upstream child has **COMPLETED** (validated, PR created) **and** published a contract.
- **Isolation:** each child has its own attempt, branch, workspace, sandbox, agent thread, validation and PR. Workspaces are never shared, and the GitHub token is scoped to that one repository and used only by worker delivery code.

## Cross-repository contracts

- **Publishing:** an upstream child whose repository has dependents is told to fill `published_contract` in its implementation report (endpoints, schemas, status codes and so on).
- **Storage:** after the child completes, the worker stores that text as an immutable `cross_repo_contract` artifact. The artifact is refused, never redacted, if it contains a secret. Its sha256 is recorded on the child.
- **Delivery to dependents:** dependent children receive the contract as **read-only context**. It is re-loaded and hash-checked for every phase run.
- **No contract:** dependents are **BLOCKED** (`upstream_contract_missing:<repository>`) instead of guessing an API.

## Failures, retries and idempotency

- **Failed child:** its dependents are BLOCKED (`upstream_failed:<repository>`). Siblings that completed keep their PRs.
- **Retry:** `uv run ai-code-change changes retry-child <child-id>` re-runs only that child, plus the children it blocked. It starts a new attempt and branch; history is kept. The parent then reports again in a new round.
- **Replays:** a replayed webhook or intake reuses the existing parent. Every status change is compare-and-set, and every child job has a dedupe key.
- **Worker crash:** the job is reclaimed when its lease expires. The child resumes its own attempt, and a stale workspace from the dead run is removed first.

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
