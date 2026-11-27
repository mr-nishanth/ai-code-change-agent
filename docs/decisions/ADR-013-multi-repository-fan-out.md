# ADR-013: Multi-repository Stories by parent/child fan-out

Status: accepted (2026-11-26)

## Decision

- **One parent per Story, one child per repository.** Each child is an ordinary attempt of the existing workflow graph (same Deep Agent, validation engine and delivery), isolated by repository, workspace, agent thread and token.
- **Coordination code is deterministic:** resolution, DAG ordering, compare-and-set state and dedupe-keyed jobs. No new agent, service, queue or Redis is added.
- **Contracts:** an upstream child publishes its contract as an immutable artifact, read-only for its dependents. A missing contract blocks the dependents.
- **All or nothing at intake:** every repository must resolve before anything runs.
- **Python repositories only:** each child runs in the Python execution image (`ai-code-change-agent/execution-python:0.2.0`) and is validated by the Python validation engine. Only Python repositories are supported; a child whose profile names another toolchain is BLOCKED (`no_execution_image_for_toolchain`).

## Rejected alternatives

- One agent writing to several repositories. This breaks per-repository isolation, token scoping and validation.
- Separate permanent agents per repository role.
