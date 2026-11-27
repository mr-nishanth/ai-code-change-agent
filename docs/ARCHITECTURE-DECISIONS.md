# Architecture decisions (owner-approved 2026-11-25)

Status: **approved**. These are the final owner decisions for the clean project. Stage B raised them as conflicts with the previous implementation; the owner approved all nine with the refinements below.

| # | Decision | As implemented |
|---|---|---|
| C1 | **PostgreSQL job queue; no Redis.** A dedicated `jobs` table; the intake job is inserted in the same transaction as the Jira webhook receipt; claiming uses `SELECT ... FOR UPDATE SKIP LOCKED`; a separate worker process; leases, heartbeat, retry limit with backoff, and recovery from worker crashes. | `persistence/models/jobs.py`, `persistence/repositories/job_queue.py`, `application/jobs/worker.py`, `persistence/repositories/webhook_repository.py` |
| C2 | **The Deep Agent may execute commands only inside its sandbox**, under these controls: non-root, read-only root filesystem, only the workspace writable, no host paths, no Docker socket, no credentials, not privileged, all capabilities dropped, Docker's default seccomp, CPU/memory/PID/disk/time limits, no network, a fresh workspace for validation, destroyed afterwards. **Dependencies:** repository-specific profiles; when dependencies cannot be prepared offline the attempt is reported **BLOCKED**, never given network access. | `execution/docker/sandbox.py`, `orchestration/steps.py::dependency_preparation` |
| C3 | **Two human gates with the same Deep Agent in two phases**: planning with read-only access, then plan approval of the exact plan hash, then implementation, then independent validation, then final approval of the exact manifest hash, then delivery. Any change to an approved artifact invalidates the approval. | `domain/changes/models.py`, `orchestration/langgraph/*` |
| C4 | **Jira reporting via application services only**: one outcome comment (draft PR URL, validation summary, attempt information). No transitions or field edits. `JIRA_REPORTING_ENABLED=false` by default. The token's comment permission is an external verification item. | `integrations/jira/reporting.py` |
| C5 | **Two LLM providers, one factory**: Anthropic (`ChatAnthropic`) for local development and tests, AWS Bedrock (`ChatBedrockConverse`) for staging and production; Settings refuse Anthropic outside development/test. Ollama and NVIDIA were tried in and removed on 2026-11-26. | `providers/llm/factory.py` |
| C6 | **Descriptive, unprefixed variable names** (`CODE_CHANGE_DELIVERY_ENABLED`, `GITHUB_PR_CREATE_ENABLED`, `JIRA_REPORTING_ENABLED`, `JIRA_PROJECT_KEYS`, `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, ...), with a documented contract. Only settings that are used exist. | `core/config.py`, [CONFIGURATION.md](CONFIGURATION.md) |
| C7 | **API and PostgreSQL in Compose; the worker as a trusted host process (`uv run`)** controlling the host Docker daemon. No socket inside sandboxes, and no worker Dockerfile yet. Future containerisation is described in [ARCHITECTURE.md](ARCHITECTURE.md#future-worker-deployment). | `compose.yaml`, `cli/operations.py` |
| C8 | **An independent PostgreSQL** (host port 5435, new volume, database and credentials), a fresh Alembic history, and LangGraph checkpoints in the same instance but in a separate schema (`langgraph`). | `compose.yaml`, `persistence/migrations/versions/20261125_0001_initial_schema.py`, `orchestration/langgraph/checkpointer.py` |
| C9 | **GitHub API-based publishing** (blobs, tree, commit, create-only ref, draft PR, re-read verification). No `git push` credential. Never merge, never touch the default branch. | `integrations/github/publisher.py` |

## SDK finding recorded during implementation

`deepagents` 0.7.41 rejects tool-level filesystem `permissions` for backends that can execute commands:

> FilesystemMiddleware does not yet support permissions with backends that provide command execution

The planning phase is therefore read-only through:

- the **read-only bind mount** of the workspace into the planning container;
- the backend refusing uploads in read-only mode.

These are OS-level guarantees and stronger than tool-level rules. Real-Docker tests prove the planning sandbox cannot change the workspace, including an attempted `write_file`.


## Amendments (2026-11-25, owner-approved)

| Decision | Amendment |
|---|---|
| C2 | Dependencies: trusted, hash-locked wheel bundles prepared outside the sandbox and mounted read-only ([ADR-010](decisions/ADR-010-dynamic-dependency-provisioning.md)). Two harness-side tools, `search_docs` and `fetch_reference`, for documentation on the exact hosts listed in `config/reference_sources.yaml` ([ADR-011](decisions/ADR-011-harness-side-reference-fetching.md)). Sandbox and validation containers remain without network. |
| C3 | The two gates remain and still bind exact hashes, but by default they are decided by deterministic policy (`WORKFLOW_APPROVAL_MODE=automatic`, actor `policy:automatic`, reasons stored). Operator approval is the `manual` mode. See [AUTOMATED-PIPELINE.md](AUTOMATED-PIPELINE.md). |
| C4 | Reporting covers failures too: one comment per attempt outcome (success with branch and draft PR; failure with stage, reason and retryability), plus one when no attempt can start (unresolvable repository). |

## Multi-repository, research and cost guards (2026-11-26)

- [ADR-013](decisions/ADR-013-multi-repository-fan-out.md): multi-repository Stories by parent/child fan-out. Deterministic coordination; no new agents or services.
- **Python-only execution:** one Python execution image (`WORKSPACE_IMAGE`) for every repository; the command policy has only the `python` family. Other languages are out of scope. See [PYTHON-EXECUTION-AND-RESEARCH.md](PYTHON-EXECUTION-AND-RESEARCH.md).
- **Documentation search:** worker-side Anthropic web search behind a `ReferenceSearcher` port. The sandbox stays offline.
- **Runaway-cost guards for any model:** a call cap, a phase USD budget and bounded structured-output retries.

## Local AWS emulator (2026-11-27)

| Decision | As implemented |
|---|---|
| [ADR-014](decisions/ADR-014-floci-local-aws-emulator.md): **Floci as a local, test-only AWS emulator.** Own Compose project and network, loopback ports, dummy credentials, never wired into the application. The only container with the Docker socket (an explicit, local-only exception). No AWS service replaces an application component; see [aws/AWS-DEPLOYMENT-READINESS.md](aws/AWS-DEPLOYMENT-READINESS.md). | `infra/docker-compose.yml`, `infra/floci/init/`, `tests/integration/floci/`, `tests/support/floci.py` |
