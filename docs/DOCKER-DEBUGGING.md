# Docker debugging

How to look inside the local stack without changing it. Everything here is read-only unless a section
says otherwise. The tools below are **optional host installs** (for example with Homebrew); they are never
added to `compose.yaml` or `infra/docker-compose.yml`.

## What runs where

| Compose project | File | Containers | Network |
|---|---|---|---|
| `ai-code-change-agent` | `compose.yaml` | `acca-api` (webhook intake, `127.0.0.1:8000`) | external `acca-floci` |
| `acca-floci` | `infra/docker-compose.yml` | `acca-floci` (DynamoDB, SQS, S3, Bedrock Runtime on `127.0.0.1:4566`) | `acca-floci` |
| none (started by Floci) | - | the web console sidecar (`127.0.0.1:4500`), the ECR registry sidecar, RDS/ECS/Lambda containers of the S2115 tests; all labelled `floci=true` | `acca-floci` |
| none (started by the worker) | - | sandboxes `aica-sbx-*`, validation and command containers `aica-<attempt>-*`, dependency `aica-resolver-*` / `aica-installer-*`, the allow-list proxy `aica-egress-*`; all labelled `aica.managed=true` | `none`, or the `--internal` `aica-dep-resolver` network |

The worker itself is a host process (`uv run ai-code-change worker`), not a container.

## Compose: status and logs

```bash
docker compose ps                                          # project ai-code-change-agent (acca-api)
docker compose logs -f --tail 200 api
docker compose -f infra/docker-compose.yml ps              # project acca-floci
docker compose -f infra/docker-compose.yml logs -f --tail 200 floci
docker inspect --format '{{json .State.Health}}' acca-floci
```

Never pass `--remove-orphans` (or run `down`) on project `ai-code-change-agent` while an `acca-postgres`
container from an older checkout must keep running: Compose counts it as an orphan of this project.
`docker compose -f infra/docker-compose.yml down -v` deletes all local DynamoDB, SQS and S3 data.

## Sandboxes and other worker containers: safe filters

Filter by label; never by a broad name pattern and never `docker rm -f $(docker ps -aq)`.

```bash
docker ps --filter label=aica.managed=true \
  --format 'table {{.Names}}\t{{.Label "aica.role"}}\t{{.Label "aica.attempt"}}\t{{.Status}}'
docker ps --filter label=aica.role=agent-sandbox            # live agent sandboxes
docker ps --filter label=aica.attempt=<attempt-id>          # everything of one attempt
docker ps --filter label=aica.role=egress                   # dependency allow-list proxy
docker ps -a --filter label=floci=true                      # containers Floci started
docker logs <aica-sbx-...>                                  # read only; sandboxes have no network
docker inspect <aica-sbx-...> --format '{{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}'
```

| Label | Set on | Meaning |
|---|---|---|
| `aica.managed=true` | every worker-started container | owned by the worker; it removes them itself |
| `aica.role` | sandboxes (`agent-sandbox`), dependency containers (`resolver`, `installer`), proxy (`egress`) | what the container does |
| `aica.attempt`, `aica.workspace` | sandboxes and command containers | the attempt and workspace ids |
| `floci=true` | containers and volumes Floci started | owned by Floci |

Do not stop or exec into a live sandbox while an attempt runs: a stopped or changed sandbox can fail the
attempt.

**Orphan sandbox after a killed worker.** A worker killed with SIGKILL (or a host crash) cannot remove
its sandbox. The container runs `sleep WORKSPACE_TIMEOUT_SECONDS` (default 7200 s) and exits on its own
after that; it has no network. When a worker reclaims the job and resumes the attempt, crash recovery
removes the attempt's old workspaces and containers first (`destroy_attempt_workspaces`). A job that
never resumes (DEAD, cancelled attempt) leaves the sandbox for the full lifetime. To find one, list
sandboxes with `docker ps --filter label=aica.role=agent-sandbox` and compare the `aica.attempt` label
with attempts no worker is running (`changes show <attempt-id>`, job not `RUNNING`); then remove only
that container: `docker rm -f $(docker ps -q --filter label=aica.attempt=<attempt-id>)`. Never remove
a sandbox whose attempt is still running.

A DEAD job no longer leaves its attempt running in the database: its on_dead hook marks the attempt
FAILED (`job_attempts_exhausted`), so such a sandbox belongs to a FAILED attempt.

Heavy Docker or Floci tests run only when `docker ps --filter label=aica.role=agent-sandbox` is empty.

## Stuck jobs, the DLQ and pending Jira reports

```bash
uv run ai-code-change jobs dlq                    # peek: job id, kind, status, attempts, last error
uv run ai-code-change reports list --state PENDING --limit 200  # among the newest 200
uv run ai-code-change reports list --state FAILED_PERMANENT  # reports Jira refused or retries ran out
uv run ai-code-change reports show attempt-<attempt-id>      # one report (never its text)
```

- `jobs dlq` never removes a message. A DLQ message of a job that is still QUEUED or RUNNING needs no
  action: the worker's sweep re-sends a message for that job once it is overdue, and the claim either
  runs it or records it DEAD. A DLQ message of a terminal job (SUCCEEDED or DEAD) can be redriven or
  purged safely; the claim deletes it and runs nothing.
- Worker log events: `job_claimed`, `job_succeeded`, `job_failed` (retry scheduled), `job_dead`
  (with `reason`), `job_dead_recovered` (a DEAD job found by the dispatcher, hook run now),
  `job_dead_hook_failed` / `job_dead_hook_abandoned`, `job_lease_released` and
  `job_shutdown_cancelled` (shutdown), `jira_report_retry_scheduled`, `jira_report_posted`,
  `jira_report_failed_permanent`. Identifiers and codes only, never payloads, Story or comment text.
- `jobs dlq` adds one to each peeked message's receive count on the DLQ (harmless: the DLQ has no
  redrive policy of its own).
- Also in the log: `job_maintenance_budget_exceeded` (a maintenance round of dead-job hooks and
  report retries ran out of its 60 s; the rest waits for the next round, the relay is not affected).
- A worker stops on SIGINT or SIGTERM within one SQS long poll (at most 20 s) plus a 30 s grace for a
  running job; a second signal cancels the job at once (its lease is released, so a restarted worker
  resumes it immediately); a third signal, or 90 s after the first, exits the process at once.
- Why a worker once ignored SIGINT for minutes (PID 37638): three things stacked. The SIGINT handler
  only set the stop event, and every further Ctrl-C did the same, so nothing could force an exit; the
  in-flight workflow job (model calls, sandbox validation) was, by design then, processed to the end;
  and after the worker returned, `asyncio.run` waits up to 300 s for executor threads still blocked
  in a Docker or AWS call. The grace, the second-signal cancel and the hard deadline now bound all
  three.
- DEAD jobs written before the dead-job hooks existed are not listed by them; see the migration note
  in [DYNAMODB-DATA-MODEL.md 8.5](aws/DYNAMODB-DATA-MODEL.md).

## lazydocker (terminal UI)

```bash
lazydocker                                                  # all containers, logs, stats, images, volumes
```

Use it to browse logs and stats. Its bulk actions (prune, remove, restart) act on the whole daemon,
including the live runtime and sandboxes: do not use them on a machine running the worker.

## ctop (live resource use)

```bash
ctop                                                        # CPU, memory, network, I/O per container
ctop -f aica                                                # only worker-started containers
```

Useful to watch a sandbox against its limits (`WORKSPACE_CPU_LIMIT`, `WORKSPACE_MEMORY_LIMIT_MB`,
`WORKSPACE_PIDS_LIMIT`).

## dive (image layers)

```bash
docker compose config --images                              # the API image name (default ai-code-change-agent-api)
dive ai-code-change-agent-api:latest                        # docker/api/Dockerfile
dive ai-code-change-agent/execution-python:0.2.0           # docker/execution/Dockerfile (the sandbox image)
CI=true dive ai-code-change-agent-api:latest                # non-interactive efficiency report
```

Check that no `.env`, key file, `var/` workspace or cache ended up in a layer, and what each layer adds.

## Floci web console

Open `http://127.0.0.1:4566/_floci/ui`. Floci starts its console sidecar on the first request (a short
"starting the console" page, then a redirect to `http://127.0.0.1:4500/`), with the image pinned in
`infra/docker-compose.yml` (`FLOCI_SERVICES_UI_IMAGE`, `floci/floci-ui:0.7.0` by digest) and published on
loopback only (`FLOCI_SERVICES_UI_BIND_ADDRESS=127.0.0.1`). Floci 2.5.0 `docs/ui/index.md`.

- It shows the local tables, items, queues, messages and buckets: handy to inspect a job item, the DLQ or an
  artifact object.
- It is **unauthenticated and can change every emulated resource**. Treat it as read-only; deleting a table,
  queue or object there loses local application state.
- Its logs: `docker logs <console container>` (find it with `docker ps --filter publish=4500`).
- It holds no real credentials and never reaches real AWS.

## AWS CLI against Floci (read-only checks)

```bash
export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1   # dummy, this shell only
aws --endpoint-url http://127.0.0.1:4566 dynamodb describe-table --table-name acca-local-app
aws --endpoint-url http://127.0.0.1:4566 sqs get-queue-attributes --attribute-names All \
  --queue-url "$(aws --endpoint-url http://127.0.0.1:4566 sqs get-queue-url --queue-name acca-local-jobs-dlq --query QueueUrl --output text)"
aws --endpoint-url http://127.0.0.1:4566 s3 ls s3://acca-local-artifacts/artifacts/ --recursive | head
```

Always pass `--endpoint-url`; never export `AWS_ENDPOINT_URL` or real credentials in a shell that also runs
the worker. Close that shell when done.
