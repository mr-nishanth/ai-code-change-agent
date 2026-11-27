# S2114 deployment readiness report

Date: 2026-11-27. No Jira Story created, no agent run, no model call, no branch or PR in connector repositories.

| Component | Status | Evidence |
|---|---|---|
| Merge | VERIFIED | PR #1 merged as `2f279f4` (31 commits, S2111–S2113); PR #2 (`pydantic.dev` host, owner-approved) merged as `1f05bd9`; local `main` = `origin/main` = `1f05bd9` |
| Pre-merge gates | VERIFIED | At `a09be22`: 1,542 passed, 0 failed/skipped/xfailed, 0 warnings (RuntimeWarning as error), coverage 91.87%; ruff, format, strict mypy clean. No CI workflows exist in the repository |
| API | VERIFIED | `acca-api` rebuilt from `main`; `/health/live` and `/health/ready` 200 locally and through ngrok; no errors in container log |
| Worker | VERIFIED | One host worker (`caffeinate -i uv run ai-code-change worker`, the documented process; there is no worker image); 2 idle DB connections from the host in `pg_stat_activity`; no queued or running jobs. Picking up a real job is NOT TESTED (would start an agent run) |
| PostgreSQL | VERIFIED | `acca-postgres` healthy, same volume `ai-code-change-agent_acca_pgdata` (no reset), Alembic `0004 (head)` = heads; 13 domain tables present |
| Docker sandbox | VERIFIED | Image `execution-python:0.2.0` present; settings: 1 CPU, 2048 MB, 256 pids, 1024 MB disk, network `none`, resolver network `aica-dep-resolver` (internal); runtime passes `--read-only`, `--cap-drop`, `--security-opt`, `--user`; the 22 Docker isolation tests passed at `a09be22` |
| ngrok and Jira webhook | VERIFIED | Tunnel `tying-spelling-customary.ngrok-free.dev` active; Jira webhook enabled, `jira:issue_created`, filter `project = KAN AND issuetype = Story`, URL `…/webhooks/jira`; a POST with a bad signature through ngrok returned 401 and stored nothing (deliveries unchanged at 5) |
| Jira REST | VERIFIED | Read-only: project KAN readable; KAN-12 returns 404 (free) |
| GitHub App | VERIFIED | Installation readable, not suspended; permissions contents write, pull requests write, metadata read; token issued; read access to automate_connector (`master` @ `f883df7`). Write not exercised (no branch or PR created) |
| Documentation registry | VERIFIED | API and worker load the same 8 exact hosts (now including `pydantic.dev`); live: `pydantic.dev/…/llms.txt` OK, `docs.pydantic.dev` → `pydantic.dev` redirect OK, `x.pydantic.dev` and `example.com` refused, a URL with a query string refused |
| Model and cost controls | VERIFIED (configuration only) | `anthropic` / `claude-haiku-5-5`, priced in the built-in table; per phase 80 calls and $2; 20 fetches and 5 searches; 3 job attempts; 1 repair. No model call made |
| Live Jira-to-GitHub E2E | NOT TESTED | Next milestone (KAN-12), needs Gate A and Gate B approval |

## Remaining blockers
- None for a single-repository run. Approvals still required: the KAN-12 Story text (Gate A) and the real-model budget (Gate B).
- Limitations: no CI in the repository; worker job pickup is only proven by the automated tests until the first live run.

## Readiness decision
**READY FOR SINGLE-REPOSITORY E2E**
