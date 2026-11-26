# Migration map

> **Historical record.** Repository names, ids and commands below are from that run. Onboarding is now generic: `register --from-provider <owner>/<repo> --project <KEY> --enable` (see AUTOMATED-PIPELINE.md); no code or document is specific to a repository.

Source: `ai-migration-agent-python`, read only. **Actions:** Reuse means the code was copied and only its imports were rewritten. Adapt means it was copied and changed. Rebuild means it was newly written for the new architecture. Excluded components are at the end. All paths are relative to `src/ai_code_change_agent/` unless they start with `tests/`.

## Jira

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `api/routes/jira_webhook.py` | `api/routes/jira_webhook.py` | Adapt | Route is now `POST /webhooks/jira` | `tests/unit/api/test_jira_webhook_api.py` |
| `api/routes/health.py`, `api/middleware/request_id.py` | same | Reuse | | `tests/unit/api/test_health.py`, `test_app_lifecycle.py` |
| `main.py` | `api/app.py`, `api/main.py` | Adapt | New settings; the API wires webhook intake only | (as above) |
| `domain/webhooks/*`, `application/webhooks/*` | same | Reuse | | `tests/unit/jira/test_receive_webhook.py`, `test_webhook_policy.py` |
| `persistence/repositories/webhook_repository.py` | same | Adapt | The intake job replaces the outbox row, in the same transaction | `tests/integration/test_webhook_repository.py`, `test_webhook_endpoint_db.py` |
| `integrations/jira/webhook.py` | `integrations/jira/cloud_verifier.py` | Reuse (renamed) | | `tests/unit/jira/test_webhook_signature.py` |
| `integrations/jira/{client,authentication,pagination,adf,mapper}.py` | same | Reuse | | `tests/unit/jira/test_client.py`, `test_authentication.py`, `test_pagination.py`, `test_adf.py`, `test_mapper.py`, `test_edge_cases.py` |
| `integrations/jira/issues.py` | `integrations/jira/adapter.py` | Reuse (renamed) | | `tests/unit/jira/test_issues.py` |
| `application/jira/{errors,models,ports,services}.py` | `application/integrations/jira/*` | Reuse (moved) | | `tests/unit/jira/test_services.py`, `test_jira_architecture.py` (adapted to allow the reporter) |
| `application/jira/story.py` | (new) | Rebuild | Extracts acceptance criteria from the Story | `tests/unit/jira/test_story_and_reporting.py` |
| `integrations/jira/reporting.py` | (new) | Rebuild | Decision C4: one outcome comment | (as above) |

## GitHub

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `integrations/github/authentication.py` | `integrations/git_providers/github_app/auth.py` | Reuse (renamed) | | `tests/unit/github/test_authentication.py` |
| `integrations/github/credentials.py`, `gitobj.py` | same | Reuse | | `tests/unit/github/test_gitobj.py` |
| `integrations/github/baseline.py` | `.../github_app/reader.py` | Reuse (renamed) | | `tests/unit/github/test_baseline_reader.py`, `test_baseline_provider.py` |
| `integrations/github/references.py` | `.../github_app/reference.py` | Reuse | | `tests/unit/repositories/test_reference_resolution.py` |
| `integrations/github/repositories.py` | `.../github_app/repository_client.py` | Reuse | | `tests/unit/repositories/test_register_from_provider.py` |
| `integrations/github/publisher.py` | `.../github_app/publisher.py` | Reuse | | `tests/unit/github/test_publisher_unit.py` (end-to-end tests now go through `tests/support/publishing.py`) |
| `application/git_providers/*` | same | Reuse | Ports for baseline, delivery and the provider client | (as above) |

## Delivery

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `application/delivery/{errors,naming,pull_request,manifest}.py` | same names | Rebuild (errors reused) | Inputs changed to the plan, changeset and validation report | `tests/unit/orchestration/test_workflow.py` |

## Registry and CLI

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `domain/repositories/*`, `application/repositories/*` | same | Reuse | | `tests/unit/repositories/*` |
| `persistence/models/registry.py`, `persistence/repositories/registry_store.py` | same | Reuse | | `tests/integration/test_registry_database.py` |
| `cli/main.py` | same | Adapt | New `worker` and `changes` commands; new settings names | `tests/unit/repositories/test_cli.py` |
| `cli/operations.py` | (new) | Rebuild | Worker and approval commands | `tests/integration/test_workflow_persistence_db.py` |

## Change domain and jobs

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `domain/changes/{states,models}.py` | `domain/changes/*` | Rebuild | New one-agent states; hash-bound approvals and safety guarantees kept | `tests/unit/changes/test_domain.py` |
| `domain/changes/errors.py` | same | Reuse | | |
| `application/changes/*`, `persistence/repositories/change_store.py`, `persistence/models/changes.py` | `application/changes/*`, `change_store.py`, `models/changes.py` | Rebuild | Simpler model; database guarantees kept | `tests/unit/changes/test_service.py`, `tests/integration/test_jobs_and_changes_db.py` |
| `application/jobs/*`, `persistence/models/jobs.py`, `persistence/repositories/job_queue.py` | (new) | Rebuild | Decision C1 | `tests/unit/jobs/test_worker.py`, `tests/integration/test_jobs_and_changes_db.py` |
| `persistence/models/webhooks.py` | same | Adapt | Request table simplified; the outbox is replaced by jobs | |
| `persistence/migrations/versions/20261125_0001_initial_schema.py` | 0001-0005 | Rebuild | Fresh history (registry DDL and triggers carried over) | `tests/integration/test_registry_database.py::test_fresh_history_roundtrip_and_head` |

## Artifacts and execution

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `application/artifacts/*` | same | Adapt | New artifact kinds; `hashing.py` moved from `application/workflows` | `tests/unit/execution/test_artifact_*` |
| `execution/artifacts.py` | `integrations/artifacts/local_fs.py` | Reuse | | `tests/unit/execution/test_artifact_store.py` |
| `application/workspaces/*` | same | Reuse | | `tests/unit/execution/test_paths_fs_policy.py` |
| `execution/{fs,baseline}.py`, `execution/docker/{process,runtime}.py` | `integrations/workspaces/*` | Reuse; runtime adapted (`tree_path`) | | `tests/unit/execution/test_docker_runtime_unit.py`, `tests/integration/test_docker_workspace.py` |
| `execution/docker/sandbox.py` | (new) | Rebuild | Deep Agents `BaseSandbox` over `docker exec` | `tests/unit/execution/test_sandbox_unit.py`, `tests/integration/test_sandbox_docker.py` |

## Agent, workflow and validation

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `agents/coding/*` | (new) | Rebuild | The one Deep Agent | `tests/unit/agents/test_coding_agent.py`, `tests/integration/test_sandbox_docker.py` |
| `providers/llm/factory.py` | `integrations/llm/*` | Rebuild | LangChain chat model (C5) | `tests/unit/orchestration/test_intake_and_composition.py` |
| `orchestration/langgraph/*`, `orchestration/{steps,intake}.py` | `application/workflows/*`, `integrations/workflow/*` | Rebuild | One parent graph, two gates | `tests/unit/orchestration/*`, `tests/integration/test_workflow_persistence_db.py` |
| `validation/*` | `application/agents/validation/*` | Rebuild (rules and approach carried over) | Input is the platform-extracted changeset | `tests/unit/validation/test_rules_and_decision.py` |

## Configuration and root files

| New path | Source path | Action | Reason | Tests migrated / added |
|---|---|---|---|---|
| `core/config.py` | same | Rebuild | New variable contract (C6) | `tests/unit/test_settings.py` |
| `core/{errors,logging}.py` | same | Reuse | | `tests/unit/test_logging.py` |
| `composition.py` | `api/composition.py` | Rebuild | One composition root for API, worker and CLI | `tests/unit/orchestration/test_intake_and_composition.py` |
| `docker/execution/*` | `docker/workspace-python/*` | Adapt | Same hash-pinned toolchain; renamed image | Docker tests |
| `docker/api/Dockerfile`, `compose.yaml` | `compose.yaml` | Rebuild | New project, port 5435, API service | Manual: health 200, unsigned webhook 401 |

## Test support

| New path | Source path | Action | Reason |
|---|---|---|---|
| `tests/support/{fake_github,jira,jira_rest,memory_repo,registry_fixtures,registry_memory}.py` | same | Reuse | |
| `tests/support/github_repos.py` | `tests/support/gate_d2.py` | Reuse (renamed) | |
| `tests/support/{changes,workflow,models,publishing}.py` | (new) | Rebuild | |

## Excluded

- **Superseded:**
  - Implementation agent and patch applier, replaced by the Deep Agent's tools.
  - 05A planning graph, prompts, schemas and stages, plus the custom LLM gateway with Anthropic, Bedrock and Ollama adapters.
  - Task-category taxonomy: copied, but unused in the new flow, so removed.
- **Not part of this architecture:**
  - Local bare-Git publisher and the disabled external boundary.
  - Empty placeholder packages (`queue`, `publishing`, `observability`).
- **History:**
  - Old migrations 0001-0005; the NestJS-era rename is not needed in a fresh database.
  - Old docs (`docs/01-12*`, NestJS inventory, module reports). They remain in the source repository.
- **Old tests tied to excluded code:** Gate C and D harness and stage tests, LLM gateway and provider contract tests, the planning workflow tests.
