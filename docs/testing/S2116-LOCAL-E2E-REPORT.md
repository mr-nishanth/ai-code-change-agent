# S2116 local end-to-end run (Task 18)

Date: 2026-11-28. Branch `feature/dynamodb-aws-alignment`. Floci 2.5.0 on `127.0.0.1:4566` (dummy
`test`/`test` credentials), Docker with `ai-code-change-agent/execution-python:0.2.0`. No network, no real
model, Jira, GitHub or AWS call.

## What runs

`tests/e2e/test_pipeline_floci.py` (markers `floci`, `docker`, `integration`), run with
`uv run pytest --no-cov -rP tests/e2e`. Each test creates its own `s2116-<uuid>` application and
checkpoint tables (from `infra/floci/schema/*.json`), job queue + DLQ and bucket, and deletes them; its
WORKSPACE_ROOT and ARTIFACT_ROOT are under `tmp_path`.

The pipeline test drives the real composition (`create_app`, `worker_app`, the operator CLI). Only these
builders are replaced (`tests/e2e/world.py`): Jira REST (the real client over a mock transport), the
GitHub baseline reader and publisher (the real adapters over `FakeGitHub`, a real bare Git repository),
the chat model (scripted tool calls; ledger provider `fake`), and the job queue's long poll (1 s) and
sweep grace (2 s). Worker timings are shortened with `model_copy` (lease 10 s, heartbeat 2 s, relay
0.5 s, sweep 1 s).

| Step | Checked |
|---|---|
| Signed webhook to the in-process ASGI app | `accepted`; the same delivery again is `duplicate`; one job item, `GSI3PK = DISPATCH` |
| Extra copy of the intake SQS message | the job runs once (`attempts = 1`) |
| Relay, claim, intake, registry resolution (seeded in DynamoDB), planning in a read-only sandbox | attempt `AWAITING_PLAN_APPROVAL` |
| Planning turn with a 450,000-byte message | offloaded to S3 (> 358,400 bytes); a fresh saver rebuilds the planning agent's state, message included |
| `changes approve --gate PLAN` (CLI) | resume job written and sent by the CLI |
| Worker SIGKILLed inside the implementation run (`tests/e2e/crash_worker.py`) | job `RUNNING` (`attempts = 1`), attempt `IMPLEMENTING`, one sandbox left running |
| Restarted worker | the same job reclaimed after the lease and `SUCCEEDED` with `attempts = 2`; the orphan sandbox removed by crash recovery; validation (pytest in the image) passed |
| `changes approve --gate FINAL`, message deleted out of band | the sweep re-sends it; delivery creates one draft PR whose commit holds the change |
| End state | three jobs `SUCCEEDED`, no GSI3 keys left, job queue and DLQ empty; every S3 artifact re-hashes to its metadata and contains the attempt's plan and manifest hashes; parent and agent checkpoints in the checkpoint table; 4 ledger rows, all provider `fake` (the killed call never answered) |

The poison test sends a non-job body and a message naming no job: both reach the DLQ after
`maxReceiveCount = 2`, the worker keeps running and no job is created.

A test is a skip with a reason when Floci is down, Docker or the image is missing, or a live
`aica.role=agent-sandbox` container runs (`tests/unit/test_e2e_support.py`).

## Results

Two consecutive runs on the final code, both green: `2 passed in 30.32s` and `2 passed in 30.28s`
(pipeline test 19.9 s each; the limit is 10 minutes), with identical unit counts. The full suite
(`uv run pytest`) passed with them: 1936 passed, coverage 93.74 %.

## DynamoDB request units per Story

Measured, not estimated: every DynamoDB data-plane call made by the API, the CLI and the in-process
workers asked for `ReturnConsumedCapacity=TOTAL` (Floci 2.5.0 returns it; checked with `GetItem`,
`PutItem`, `UpdateItem`, `Query`, `BatchGetItem` and `TransactWriteItems` before use). Counted in
`tests/e2e/capacity.py`; test inspection reads are not counted. The SIGKILLed subprocess is a separate
process and is not counted either; its work (one claim, one transition, one checkpointed model step) is
redone and counted in the restarted worker, so the numbers are close to one Story without a crash.

| Table | Read request units | Write request units |
|---|---:|---:|
| `acca-app` (requests, attempts, jobs, registry, ledger) | 157.5 | 142 |
| `acca-checkpoints` (parent workflow + 3 agent runs) | 72 | 262 |
| Total | 229.5 | 404 |
| of which outbox relay/sweep polling (GSI3 `Query`, depends on worker uptime, not on the Story) | 25 | 0 |
| **Per Story** (total minus polling) | **204.5** | **404** |

| Operation | Table | Index | Requests | Units |
|---|---|---|---:|---:|
| `BatchGetItem` | app | - | 22 | 23 |
| `GetItem` | app | - | 89 | 89 |
| `Query` | app | - | 20 | 20 |
| `Query` | app | GSI1 | 1 | 0.5 |
| `Query` | app | GSI3 | 50 | 25 |
| `TransactWriteItems` | app | - | 25 | 123 |
| `UpdateItem` | app | - | 11 | 19 |
| `GetItem` | checkpoints | - | 49 | 49 |
| `PutItem` | checkpoints | - | 225 | 262 |
| `Query` | checkpoints | - | 23 | 23 |
| `DescribeTable` (worker start, 3 workers x 2 tables) | both | - | 6 | 0 |

Other requests in the same run: SQS 23 (`ReceiveMessage` 10, `SendMessage` 4, `DeleteMessage` 4,
`GetQueueUrl` 5); S3 120 (`GetObject` 83, `PutObject` 17, `ListObjectsV2` 15, lifecycle 5).

The checkpoint table takes 65 % of the write units: LangGraph writes a checkpoint and its pending writes
on every graph step of the parent workflow and of each agent run. A real Story with more model turns
writes more checkpoints; the app table's share grows only with state transitions.

**Estimated DynamoDB cost per Story (on-demand):**

`cost ≈ 204.5 × price_per_RRU + 404 × price_per_WRU`

with the region's current on-demand prices (aws.amazon.com/dynamodb/pricing, "on-demand capacity"),
plus storage (GB-month, indexes included) and `25 RRU per ~20 s of three workers' polling` scaled to the
real relay/sweep intervals (DYNAMODB-DATA-MODEL.md section 13: about 1,584 RRU per worker per day at the
defaults). Caveats: units are as Floci reports them; whether Floci adds global secondary index write
units the way AWS does was not verified, so treat WRU as a lower bound for the app table. The scripted
Story has 2 planning and 2 implementation model turns; a real Story's checkpoint writes scale with its
model and tool turns.

## Findings

- A worker SIGKILL leaves the sandbox container running (`sleep` for the sandbox lifetime). The
  restarted worker's crash recovery (`destroy_attempt_workspaces` for the same attempt) removed it.
  Until a restart picks the attempt up again, the container counts as a live sandbox, so this suite
  skips.
- LangGraph warns when the planning agent's checkpoint is read back: `Deserializing unregistered type
  ai_code_change_agent.agents.coding.schemas.ImplementationPlan from checkpoint. This will be blocked in
  a future version` (`LANGGRAPH_STRICT_MSGPACK` / `allowed_msgpack_modules`). Today the read works;
  a future LangGraph release would refuse it unless the type is allow-listed.
- The agent's checkpoints do not hold the full message list in `channel_values` (LangGraph 1.2 rebuilds
  it from the pending writes); the offloaded object is the 450 KB pending write of the planning turn.
  Reading the state through the agent graph (`aget_state`) returns the complete history.
