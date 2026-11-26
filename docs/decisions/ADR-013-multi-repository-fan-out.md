# ADR-013: Multi-repository Stories by parent/child fan-out

Status: accepted (2026-11-26)

## Decision

- **One parent per Story, one child per repository.** Each child is an ordinary attempt of the existing workflow graph (same Deep Agent, validation engine and delivery), isolated by repository, workspace, agent thread and token.
- **Coordination code is deterministic:** resolution, DAG ordering, compare-and-set state and dedupe-keyed jobs. No new agent, service, queue or Redis is added.
- **Contracts:** an upstream child publishes its contract as an immutable artifact, read-only for its dependents. A missing contract blocks the dependents.
- **All or nothing at intake:** every repository must resolve before anything runs.

## Rejected alternatives

- One agent writing to several repositories. This breaks per-repository isolation, token scoping and validation.
- Separate permanent agents per repository role.
