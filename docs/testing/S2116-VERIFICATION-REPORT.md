# S2116 verification: isolated ports, no PostgreSQL

Final verification of the DynamoDB + SQS + S3 persistence (ADR-015) on branch
`feature/dynamodb-aws-alignment` (base commit `c5e6ebc`), run on 2026-11-27 (UTC) on the developer machine
**next to** the running local stack, which was not touched. A second Floci and the API ran on
isolated ports, network and volume (`infra/verify/docker-compose.override.yml`, compose project
`acca-verify`). The worker ran on the host. No real model, Jira or GitHub call was made.

## Result

| Check | Result |
|---|---|
| Verify Floci `acca-verify-floci` on 127.0.0.1:4577 only, network `acca-verify`, volume `acca_verify_floci_data`, no Docker socket, no console | yes |
| Bootstrap with `ACCA_FLOCI_ENDPOINT_URL=http://127.0.0.1:4577` | complete (queues + DLQ, buckets, tables + TTL) |
| API `acca-verify-api` on 127.0.0.1:18000, network `acca-verify` only (no network shared with `acca-postgres`) | yes |
| `docker exec acca-verify-api python -c "import psycopg"` | `ModuleNotFoundError: No module named 'psycopg'` |
| `acca-postgres` from inside the API | `socket.gaierror: Name or service not known` |
| `/health/live`, `/health/ready` | 200, 200 (`{"dynamodb":true}`) |
| Signed synthetic webhook | 200 `accepted`; same delivery again `duplicate`; bad signature 401; delivery, request, job (`GSI3PK=DISPATCH`) items in the verify table |
| Worker, `DATABASE_URL` unset | intake job SUCCEEDED, attempt `AWAITING_PLAN_APPROVAL` (plan gate) in 17 s |
| Worker, `DATABASE_URL=postgresql://127.0.0.1:1/none` exported | ignored; second Story to the plan gate in 17 s |
| `ruff check`, `ruff format --check`, `mypy` | pass |
| `pytest` (full suite, Floci tests on the verify Floci) | 1929 passed, 4 failed, 3 errors: all explained below, none in DynamoDB/SQS/S3 code |
| `pytest tests/e2e` on the verify Floci | 2 passed |
| Teardown `-p acca-verify down -v --rmi local` | containers, network, volume and image gone; ports 4577 and 18000 free |
| Live runtime before/after | identical (start times, PIDs, health) |

## Commands and trimmed output

All commands ran from the worktree root, which has no `.env`. Each process started with `env -i`
and only the variables shown. No `ANTHROPIC_API_KEY`, `AWS_*` or other real credentials were set.
`JIRA_WEBHOOK_SECRET` was a random 64-hex value generated for this run, `<synthetic>` below.

```sh
DC="docker compose -p acca-verify --project-directory . -f compose.yaml -f infra/docker-compose.yml \
  -f infra/verify/docker-compose.override.yml"
CENV="env -i PATH=... HOME=$HOME ACCA_API_CONTAINER_NAME=acca-verify-api ACCA_API_PORT=18000 \
  JIRA_WEBHOOK_SECRET=<synthetic> JIRA_PROJECT_KEYS=SHOP APP_ENV=development"
```

### 1. Live state (before)

`docker inspect` + `pgrep` + `curl`; the full before/after table is in
[Live runtime](#live-runtime-before-and-after). The live API (an older build) serves `/health/live`;
`/health` is 404 there both before and after, so the health check is on `/health/live`.

### 2. Isolated Floci

```text
$ $CENV $DC config        # merged: floci ports [127.0.0.1:4577:4566], volumes [acca_verify_floci_data:/app/data],
                          # networks [acca-verify], FLOCI_SERVICES_UI_ENABLED "false"; no /var/run/docker.sock
$ $CENV $DC up -d --wait floci
 Network acca-verify Created / Volume acca_verify_floci_data Created
 Container acca-verify-floci Started / Healthy
$ docker inspect acca-verify-floci
/acca-verify-floci net=acca-verify mounts=volume:acca_verify_floci_data->/app/data ports={"4566/tcp":[{"HostIp":"127.0.0.1","HostPort":"4577"}]}
$ curl 127.0.0.1:4577/_floci/health    {"version":"2.5.0","edition":"community",...}
$ env -i PATH=... HOME=$HOME ACCA_FLOCI_ENDPOINT_URL=http://127.0.0.1:4577 sh infra/floci/init/bootstrap.sh
floci-init: sqs queue acca-local-jobs-dlq created
floci-init: sqs queue acca-local-jobs created
floci-init: sqs queue acca-local-jobs set: VisibilityTimeout 300, ReceiveMessageWaitTimeSeconds 20, redrive to ...:acca-local-jobs-dlq after 20 receives
floci-init: s3 bucket acca-local-artifacts created
floci-init: s3 bucket acca-local-scratch created
floci-init: ecr repository acca/local-smoke created           # metadata only: this Floci has no Docker
floci-init: secret acca/local/test-only-dummy created
floci-init: log group /acca/local/worker created (retention 1 day), /acca/local/api created (retention 1 day)
floci-init: dynamodb table acca-local-app created from acca-app.json; ttl enabled on expires_at
floci-init: dynamodb table acca-local-checkpoints created from acca-checkpoints.json; ttl enabled on ttl
floci-init: bootstrap complete
```

### 3. API without PostgreSQL

```text
$ $CENV $DC up -d --build --wait api
 Image acca-verify-api:s2116-verify Built / Container acca-verify-api Started
$ docker inspect acca-verify-api acca-postgres
/acca-verify-api net=acca-verify mounts=[] ports={"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"18000"}]} ro=true capdrop=[ALL]
/acca-postgres   net=ai-code-change-agent_default ...
$ docker exec acca-verify-api env | grep -E '^(DATABASE_URL|ACCA_|APP_ENV)'
ACCA_AWS_ENDPOINT_URL=http://acca-verify-floci:4566
ACCA_DYNAMODB_TABLE_NAME=acca-local-app
APP_ENV=development
$ docker exec acca-verify-api python -c "import psycopg"
ModuleNotFoundError: No module named 'psycopg'                                    (exit 1)
$ docker exec acca-verify-api python -c "import socket; socket.create_connection(('acca-postgres',5432),3)"
socket.gaierror: [Errno -2] Name or service not known                             (exit 1)
$ curl 127.0.0.1:18000/health/live     {"status":"alive","version":"0.1.0"} HTTP 200
$ curl 127.0.0.1:18000/health/ready    {"status":"ready","dependencies":{"dynamodb":true}} HTTP 200
```

The registry was seeded in the verify table: repository `shop-service`, profile v1 published, project
`SHOP` mapped, enabled. A scripted local bare repository stood in for GitHub. Then the webhook, signed
with HMAC-SHA256 over the body and sent from the host to `http://127.0.0.1:18000/webhooks/jira`:

```text
SHOP-1 verify-delivery-0001 200 {"status":"accepted","deliveryId":"verify-delivery-0001"}
bad signature -> 401
SHOP-1 verify-delivery-0001 200 {"status":"duplicate","deliveryId":"verify-delivery-0001"}
$ aws --endpoint-url http://127.0.0.1:4577 dynamodb scan --table-name acca-local-app   (test/test creds)
DELIVERY#verify-delivery-0001                         DELIVERY
JIRAISSUE#10001                                       GUARD
JOBKEY#code_change.intake:3c852b92-...                GUARD
JOB#5addb847-...                       JOB  code_change.intake  QUEUED  GSI3PK=DISPATCH
REQ#3c852b92-...                       REQUEST                  RECEIVED
(+ the registry items: REPO, ALIAS, PV#000001, PROJECT#SHOP MAP, PROVIDERREPO, LOCATOR, AUDIT)
```

### 4. Worker to the plan gate (host, scripted model)

The CLI worker has no stub-model mode, and intake needs Jira REST and the GitHub baseline. So a
scratchpad runner (not committed) installed the `tests/e2e/world.py` fakes first: a scripted
tool-calling model, a mock-transport Jira that serves the synthetic Story, and `FakeGitHub` over the
local bare repository. Then it called the real CLI entry point
`ai_code_change_agent.cli.main.main(["worker"])`, which is what `ai-code-change worker` runs. As a
guard, `HTTP(S)_PROXY` pointed at a dead loopback port, with `NO_PROXY=127.0.0.1,localhost`, so any
stray outbound HTTP call would have failed.

```text
env -i PATH=... HOME=$HOME APP_ENV=development ACCA_AWS_ENDPOINT_URL=http://127.0.0.1:4577 \
  WORKSPACE_ROOT=<scratch>/workspaces ARTIFACT_ROOT=<scratch>/artifacts \
  WORKSPACE_IMAGE=ai-code-change-agent/execution-python:0.2.0 WORKFLOW_APPROVAL_MODE=manual \
  LLM_PROVIDER=anthropic LLM_MODEL=fake-scripted ANTHROPIC_API_KEY=sk-test-never-used \
  REFERENCE_SEARCH_ENABLED=false MAX_CONCURRENT_JOBS=1 HTTPS_PROXY=http://127.0.0.1:9 ... \
  uv run python <scratch>/verify_runner.py worker            # DATABASE_URL not set

verify-runner: DATABASE_URL unset | endpoint http://127.0.0.1:4577
[info] job_dispatcher_sent            count=1 step=dispatch_pending
[info] jira_request                   operation=get_issue status=200      (fake transport)
[info] workspace_created              attempt_id=0c4f4065-... files=4
[info] job_succeeded                  job_id=5addb847-... kind=code_change.intake
status: {"attempt": "0c4f4065-...", "status": "AWAITING_PLAN_APPROVAL", "plan_sha256": "cda03dda...", "base_commit": "98948f50..."}
(SIGINT) verify-runner: worker exit 0 | model turns left 0 | jira calls ['/rest/api/3/issue/SHOP-1'] | github foreign requests []
```

Second run: a second Story (`SHOP-2`, delivery `verify-delivery-0002`, accepted), and
`DATABASE_URL=postgresql://127.0.0.1:1/none` exported in the worker's environment:

```text
verify-runner: DATABASE_URL set (ignored) | endpoint http://127.0.0.1:4577
[info] job_succeeded                  job_id=fabb52a1-... kind=code_change.intake
status: {"attempt": "7d6230ab-...", "status": "AWAITING_PLAN_APPROVAL", ...}
(SIGINT) verify-runner: worker exit 0 | model turns left 0 | jira calls ['/rest/api/3/issue/SHOP-2'] | github foreign requests []
```

State afterwards: both intake jobs `SUCCEEDED` (attempts 1, out of the outbox index); both requests
and attempts `AWAITING_PLAN_APPROVAL`; job queue and DLQ empty; 190 checkpoint items; plan and
workspace artifacts in `s3://acca-local-artifacts/artifacts/<request>/<attempt>/`; each planning
checkpoint offloaded to S3 (`checkpoints/attempt:...:planning:.../writes/.../0`, 450,415 bytes); no
sandbox left behind.

### 5. Gates

```text
$ env -i PATH=... HOME=$HOME ACCA_FLOCI_ENDPOINT_URL=http://127.0.0.1:4577 sh -c \
    'uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -p no:cacheprovider -q -rs'
All checks passed!
384 files already formatted
Success: no issues found in 345 source files
Required test coverage of 90% reached. Total coverage: 93.74%
4 failed, 1929 passed, 3 errors in 263.53s (0:04:23)

$ ... uv run pytest tests/e2e -p no:cacheprovider -q -rs -rP --no-cov
E2E runtime 19.9s; sandboxes left by the SIGKILL: 1 (removed by the restarted worker's crash recovery)
{"rru": 229.5, "wru": 404.0, "dynamodb_requests": 521, "sqs_requests": 23, "s3_requests": 120}
2 passed in 30.36s
```

No test was skipped: no live sandbox (`aica.role=agent-sandbox`) was running. The 7 non-passes come
from the verification setup, not from the code under test:

| Tests | Cause |
|---|---|
| `tests/integration/floci/test_bedrock_proxy.py` (3 setup errors) | The test starts its own fake upstream on `127.0.0.1:4577`, the port this run gave the verify Floci: `Bind for 127.0.0.1:4577 failed: port is already allocated`. Re-run after teardown, with 4577 free: **3 passed**. |
| `test_ecr_push_and_pull_by_digest`, `test_ecs_task_exit_code_and_stop`, `test_rds_postgres_create_connect_query_delete`, `test_lambda_invoke_and_api_gateway_http_and_rest` (4 failed) | S2115 tests of Floci's container-backed services. The verify Floci deliberately has no Docker socket: `The ECR backing registry is unavailable because no Docker daemon is reachable from Floci`. They need the development Floci (`infra/docker-compose.yml`, with the socket). They were not run against the live `acca-floci`, which must not be touched. They are unchanged by S2116 and passed in the Task 18 full gate (1936 passed). |

## Teardown

```text
$ $CENV $DC down -v --rmi local
 Container acca-verify-api Removed / Container acca-verify-floci Removed
 Volume acca_verify_floci_data Removed / Network acca-verify Removed / Image acca-verify-api:s2116-verify Removed
no acca-verify containers / no acca-verify network / no acca_verify volume / no acca-verify image / 4577 free / 18000 free
```

The verify worker had already stopped (SIGINT, exit 0). The image was first built as compose's
default `acca-verify-api:latest`. The override now names it `acca-verify-api:s2116-verify`, and steps
2 and 3 were run again with the final file (Floci up and bootstrap, API build, both health checks 200,
`psycopg` missing, signed webhook `SHOP-3` accepted with its items in the table, teardown clean).

## Live runtime before and after

Before: 2026-11-27T23:45:14Z. After: 2026-11-28T00:00:43Z.

| Item | Before | After |
|---|---|---|
| `acca-api` | running, started 2026-11-27T08:08:43.081390425Z, pid 94837, id ce23de4aa2b3 | same |
| `acca-postgres` | running (healthy), started 2026-11-27T04:39:23.369161666Z, pid 719, id 59ec4ce0d8fe | same |
| `acca-floci` | running (healthy), started 2026-11-27T10:09:16.386488092Z, pid 82877, id 2459314d222b | same |
| `floci-aws-ui` | running, started 2026-11-27T10:09:52.707795512Z, pid 83444, id 568c6feb4004 | same |
| `floci-aws-ecr-registry` | running, started 2026-11-27T08:28:07.349255963Z, pid 1450, id 8ae7bf3b79b5 | same |
| host worker (`caffeinate -i uv run ai-code-change worker`) | PIDs 73696 (shell), 73697 (uv), 73698 (caffeinate), 73699 (python), started Fri Nov 27 13:38:49/50 2026 | same |
| ngrok (`ngrok http 8000`) | PID 20394, started Fri Nov 27 10:09:40 2026 | same |
| `127.0.0.1:8000/health/live` | 200 `{"status":"alive","version":"0.1.0"}` | 200, same body |
| `127.0.0.1:4566/_floci/health` | 200 | 200 |
| agent sandboxes | 0 | 0 |
| networks, volumes | 12 networks, 4 volumes (no `acca-verify*`) | identical lists |

## Notes

- The brief named `/health` and `/ready`; the application's routes are `/health/live` and
  `/health/ready` (`src/ai_code_change_agent/api/routes/health.py`). Those were checked.
- `compose.yaml` hard-codes `ACCA_AWS_ENDPOINT_URL: http://acca-floci:4566` and the external
  `acca-floci` network. The override replaces both for the api service (`environment` merge,
  `networks: !override`), so `compose.yaml` did not need to change.
- Run 2's usage ledger has no rows of its own: the scripted model reuses fixed provider message ids
  (`msg_fake_0001`, `msg_fake_0002`) and the ledger deduplicates by message id. This comes from
  replaying the same script twice into one table. It is not a product behaviour change.
- Docker-backed Floci services (ECR images, ECS, RDS, Lambda) cannot work on the verify Floci, because
  it has no Docker socket. The application does not use them.

## S2117 re-run (2026-11-28)

The same procedure, repeated on `feature/dynamodb-aws-alignment` HEAD `fb00fa1` (2026-11-28 04:27-04:32 UTC),
next to the running local stack, which was not touched. Same compose project (`acca-verify`), override
and isolation; a fresh random 64-hex `JIRA_WEBHOOK_SECRET` (`<synthetic>`); every process under `env -i`
with only the variables shown; no `.env` in the worktree; no real model, Jira, GitHub or AWS call.

| Check | Result |
|---|---|
| `$DC config`: floci `127.0.0.1:4577`, api `127.0.0.1:18000`, network `acca-verify`, volume `acca_verify_floci_data`, no `docker.sock`, no `:latest` | yes |
| Verify Floci up + bootstrap on `http://127.0.0.1:4577` | healthy; `bootstrap complete` (queues + DLQ, buckets, tables + TTL) |
| `acca-verify-api` (`acca-verify-api:s2116-verify`) network `acca-verify` only, no mounts, `ro=true`, `capdrop=[ALL]`; `acca-postgres` on `ai-code-change-agent_default` | yes |
| `DATABASE_URL` in the API container | not set (env: `ACCA_AWS_ENDPOINT_URL`, `ACCA_DYNAMODB_TABLE_NAME`, `APP_ENV` only) |
| `python -c "import psycopg"` / `"import sqlalchemy"` in the container | `ModuleNotFoundError` / `ModuleNotFoundError` (exit 1) |
| `acca-postgres:5432` from the container | `socket.gaierror: [Errno -2] Name or service not known` |
| `/health/live`, `/health/ready` | 200 `{"status":"alive",...}`, 200 `{"status":"ready","dependencies":{"dynamodb":true}}` |
| Signed webhook `SHOP-1` / same delivery again / bad signature / `SHOP-2` | 200 `accepted` / 200 `duplicate` / 401 / 200 `accepted` |
| Items after `SHOP-1` | `DELIVERY#verify-delivery-0001`, `JIRAISSUE#10001` guard, `JOBKEY#...` guard, `JOB#23dd9558-...` `code_change.intake QUEUED GSI3PK=DISPATCH`, `REQ#eefd6bbc-...` `RECEIVED` (+ registry items) |
| Worker, `DATABASE_URL` unset | intake `SUCCEEDED`; attempt `f4fea4b7-...` `AWAITING_PLAN_APPROVAL`; runner wall time 7.9 s |
| Worker, `DATABASE_URL=postgresql://127.0.0.1:1/none` exported | ignored; intake `SUCCEEDED`; attempt `eee995ef-...` `AWAITING_PLAN_APPROVAL`; 8.0 s |
| `grep -rnEi 'psycopg\|sqlalchemy\|alembic\|DATABASE_URL' src/` | no matches (exit 1) |
| Teardown `-p acca-verify down -v --rmi local` | containers, network, volume, image removed; 4577 and 18000 free; no verify runner left |
| Live runtime before/after | identical (below) |

Worker runs (scratchpad runner, not committed; same design as S2116: `tests/e2e/world.py` fakes installed
with `install(world, setattr)`, a `FakeJira` subclass serving `SHOP-<n>`, `FakeGitHub` over a local bare
repository, scripted planning model, then the real `ai_code_change_agent.cli.main.main(["worker"])`;
a watcher thread sends SIGINT once a new attempt reaches the plan gate):

```text
env -i PATH=... HOME=$HOME APP_ENV=development ACCA_AWS_ENDPOINT_URL=http://127.0.0.1:4577 \
  PYTHONPATH=<worktree> VERIFY_SCRATCH=<scratch> LOG_JSON=false \
  HTTP(S)_PROXY=http://127.0.0.1:9 NO_PROXY=127.0.0.1,localhost \
  uv run python <scratch>/verify_runner.py worker 1           # the runner fills the world.py settings
                                                              # (dummy key sk-test-never-used, fake Jira/GitHub)
verify-runner: DATABASE_URL unset | endpoint http://127.0.0.1:4577
[info] job_dispatcher_sent   count=1 step=dispatch_pending
[info] jira_request          operation=get_issue status=200          (mock transport, twice)
[info] workspace_created     attempt_id=f4fea4b7-... files=4
[info] job_succeeded         job_id=23dd9558-... kind=code_change.intake
status: {"attempt": "f4fea4b7-...", "status": "AWAITING_PLAN_APPROVAL", "plan_sha256": "cda03dda...", "base_commit": "98948f50..."}
verify-runner: worker exit 0 | model turns left 0 | jira calls ['/rest/api/3/issue/SHOP-1', '/rest/api/3/issue/SHOP-1']

... DATABASE_URL=postgresql://127.0.0.1:1/none uv run python <scratch>/verify_runner.py worker 2
verify-runner: DATABASE_URL set (ignored) | endpoint http://127.0.0.1:4577
[info] job_succeeded         job_id=a304dafd-... kind=code_change.intake
status: {"attempt": "eee995ef-...", "status": "AWAITING_PLAN_APPROVAL", "plan_sha256": "cda03dda...", "base_commit": "98948f50..."}
verify-runner: worker exit 0 | model turns left 0 | jira calls ['/rest/api/3/issue/SHOP-2', '/rest/api/3/issue/SHOP-2']
```

State afterwards: both intake jobs `SUCCEEDED` and out of the outbox index; both requests and attempts
`AWAITING_PLAN_APPROVAL`; job queue and DLQ 0/0; 190 checkpoint items; plan and workspace artifacts under
`s3://acca-local-artifacts/artifacts/<request>/<attempt>/`; each planning checkpoint write offloaded to S3
(`checkpoints/attempt:...:planning:.../writes/.../0`, 450,415 bytes); 0 agent sandboxes before, between
and after the runs.

Live runtime, before 2026-11-28T04:27:39Z, after 2026-11-28T04:31:59Z:

| Item | Before | After |
|---|---|---|
| `acca-api` | running, started 2026-11-28T03:33:39.750908887Z, pid 1619, id ce23de4aa2b3 | same |
| `acca-postgres` | running (healthy), started 2026-11-28T03:32:44.969889333Z, pid 745, id 59ec4ce0d8fe | same |
| `acca-floci` | running (healthy), started 2026-11-28T03:32:44.967532916Z, pid 746, id 2459314d222b | same |
| `floci-aws-ui` | running, started 2026-11-28T03:33:39.821396262Z, pid 1685, id 568c6feb4004 | same |
| `floci-aws-ecr-registry` | running, started 2026-11-28T03:33:39.905340137Z, pid 1753, id 8ae7bf3b79b5 | same |
| host worker (`caffeinate -i uv run ai-code-change worker`) | PIDs 7890 (shell), 7891 (uv), 7892 (caffeinate), 7895 (python), started Sat Nov 28 09:03:46 2026 | same |
| ngrok (`ngrok http 8000`) | PIDs 8510 (shell), 8511, started Sat Nov 28 09:04:03 2026 | same |
| `127.0.0.1:8000/health/live` | 200 `{"status":"alive","version":"0.1.0"}` | 200, same body |
| `127.0.0.1:4566/_floci/health` | 200 | 200 |
| agent sandboxes | 0 | 0 |
| networks, volumes | 12 networks, 4 volumes (no `acca-verify*`) | identical lists |

Notes:

- The live stack was restarted between S2116 and this run (new start times and PIDs, same container ids);
  that restart predates this run and is not part of it.
- Intake now reads the Story twice from the fake Jira (`get_issue` x2 per run; S2116 recorded one call).
  Both calls go to the mock transport; no network call is involved.
- The worker's session cost summary reports `2 call(s) without usage` for the scripted model, so the cost
  is "at least $0.0000". The scripted turns carry `usage_metadata`; this is a reporting detail of the fake
  model run, not a PostgreSQL concern.
