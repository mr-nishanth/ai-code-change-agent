# How the project works (end to end)

AI Code Change Agent turns a Jira Story into reviewed-ready **draft pull requests**. It uses one LangGraph workflow, one Deep Agent (the coding agent) and one independent Python validation engine. No PR is ever merged automatically.

## Components

| Component | Where | Does |
|---|---|---|
| API (FastAPI) | `api/`, container `acca-api` | Receives the Jira webhook, verifies its HMAC signature, de-duplicates it and queues an intake job. Holds only the webhook secret |
| DynamoDB, SQS, S3 | AWS; locally the Floci container `acca-floci` | DynamoDB: requests, attempts, approvals, job state and outbox, registry, parent/child executions, LLM usage ledger, LangGraph checkpoints. SQS (+ DLQ): job delivery. S3: artifacts and large checkpoint payloads ([ADR-015](decisions/ADR-015-dynamodb-sqs-s3-persistence.md)) |
| Worker | `uv run ai-code-change worker` (host process) | The trusted control plane: runs jobs, the workflow, Docker sandboxes, validation, GitHub delivery and Jira reporting. Holds the credentials |
| Repository registry | `application/repositories`, CLI `ai-code-change repositories` | Which repositories are allowed, for which Jira projects, with which execution profile (toolchain, test command, limits) |
| Coding agent | `agents/coding` (Deep Agents) | Plans (read-only sandbox), then implements (writable sandbox). Has no network and no credentials |
| Validation engine | `validation/` | Rebuilds a fresh workspace from the pinned commit plus the changeset and checks paths, limits and secrets. Runs the profile's test command offline and decides PR eligibility |
| Multi-repo coordinator | `application/multirepo` | One child per repository of a Story: ordering, contracts, aggregation |
| Deep Agent Studio | `studio/` + `deep-agent-ui/` (dev only) | React UI: run the agent interactively, view history and watch pipeline attempts live |

## Single-repository Story

```
Jira Story created ("Repository: owner/repo")
  -> webhook -> API: verify HMAC, dedupe, store, queue "intake" job
  -> worker intake: re-read the Story from Jira (payload is never trusted)
       -> resolve the repository in the registry (ACTIVE, mapped to the project, published profile)
       -> create attempt -> start the LangGraph workflow (checkpointed in DynamoDB)
  1 prepare    pin the default branch to one commit (GitHub App token scoped to this repository)
  2 plan       Deep Agent in a read-only, offline sandbox -> structured ImplementationPlan
               (may use search_docs / fetch_reference, which run in the worker)
  3 PLAN gate  automatic policy: safe paths, no protected files, limits, valid dependency
               request, a test command exists -> approved (bound to the plan's hash)
  4 deps       if the plan asks for packages: a trusted resolver container downloads hash-locked
               wheels -> read-only bundle mounted into the offline sandbox (ADR-010)
  5 implement  Deep Agent in a writable offline sandbox: edits, runs tests, reports
  6 validate   independent engine: fresh rebuild, checks, test command offline
               -> failed code -> one bounded correction run (MAX_REPAIR_ATTEMPTS) -> validate again
               -> transient Git host error -> retried, never sent to the agent
  7 FINAL gate automatic policy: validation passed, manifest matches the plan
  8 deliver    create branch + commit + DRAFT PR through the GitHub App (create-only, never merge)
  9 report     one Jira comment with the PR link; LLM cost printed in the worker terminal
```

## Multi-repository Story

Several `Repository:` lines (with optional `Role`, `Responsibility` and `Depends on`) create a parent with one child per repository:
- **Before anything runs:** every repository must resolve, and the dependency graph must have no cycle. Otherwise nothing runs and Jira gets one comment explaining why.
- **Isolation:** each child runs the full single-repository flow above, in its own workspace, sandbox, agent thread and branch, with its own PR.
- **Order:** children start in dependency order. An upstream child publishes its API contract (an immutable, hashed artifact), and its dependents receive it as read-only context.
- **Reporting:** one aggregated Jira comment lists every repository, PR and outcome. `changes retry-child` re-runs one failed child.

Details: [MULTI-REPOSITORY.md](MULTI-REPOSITORY.md).

## Safety in one place

- **Access:** repository access requires both the GitHub App installation and the registry. Story text never grants access.
- **Sandboxes:** no network, non-root user, read-only root filesystem, no capabilities, no Docker socket and no secrets. Dependencies come only from verified, read-only bundles.
- **Validation:** independent of the model. Only validated changes are delivered, always as draft PRs.
- **Cost:**
  - every model call is recorded and priced;
  - per phase run there are hard caps on model calls and USD, and bounded retries;
  - reports are available per stage, attempt, repository, Story and worker session.

## Where to go next

- **Set up and run:** [GETTING-STARTED.md](GETTING-STARTED.md).
- **Configuration:** [CONFIGURATION.md](CONFIGURATION.md).
- **Security:** [SECURITY.md](SECURITY.md).
- **Architecture and decisions:** [ARCHITECTURE.md](ARCHITECTURE.md), [ARCHITECTURE-DECISIONS.md](ARCHITECTURE-DECISIONS.md).
