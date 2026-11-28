# DynamoDB data model, job model and checkpointer

Status: **approved by the owner** (2026-11-27). Branch `feature/dynamodb-aws-alignment`. DynamoDB starts
empty: no data is migrated from PostgreSQL, there is no dual-write period, no backend flag and no
PostgreSQL fallback. PostgreSQL leaves the runtime in one cut (ADR-015). Implementation audit:
[S2116-IMPLEMENTATION-AUDIT.md](S2116-IMPLEMENTATION-AUDIT.md).
Inputs: [DYNAMODB-ACCESS-PATTERNS.md](DYNAMODB-ACCESS-PATTERNS.md) (every access pattern,
with file:line), live probes against Floci 2.5.0 on `127.0.0.1:4566` with dummy credentials (section 10),
and the Floci documentation at tag `2.5.0` (`github.com/floci-io/floci`, `docs/services/*.md`, cited as
`file:line`). Nothing here touches real AWS, GitHub, Jira or a model.

Owner requirements this design meets: DynamoDB is the only application database (no PostgreSQL, RDS,
SQLAlchemy or Alembic at runtime); SQS with a DLQ delivers jobs and DynamoDB holds their durable
state; S3 holds large artifacts; the local Docker sandbox stays; no Redis, no Kubernetes, no agent
roles, no static repository allowlist; no one-to-one table conversion.

## 1. Resources

| Resource | Name (setting) | Purpose |
|---|---|---|
| DynamoDB table | `acca-<env>-app`, locally `acca-local-app` (`ACCA_DYNAMODB_TABLE_NAME`); called `acca-app` below | All domain state, registry, usage ledger, job state, uniqueness guards, outbox |
| DynamoDB table | `acca-<env>-checkpoints`, locally `acca-local-checkpoints` (`ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME`); called `acca-checkpoints` below | LangGraph checkpoints, schema owned by `langgraph-checkpoint-aws` (`PK`/`SK` strings, TTL attribute `ttl`) |
| SQS standard queue | `acca-<env>-jobs`; locally the existing S2115 queue `acca-local-jobs` (`ACCA_SQS_JOB_QUEUE_NAME`) | Job delivery: body `{"job_id": "<uuid>"}` only |
| SQS standard queue | `acca-<env>-jobs-dlq`; locally the existing `acca-local-jobs-dlq` | Poison messages (`maxReceiveCount` 20) |
| S3 bucket | `acca-<env>-artifacts`; locally the existing `acca-local-artifacts` (`ACCA_S3_BUCKET_NAME`) | Prefix `artifacts/` (attempt artifacts), prefix `checkpoints/` (checkpoint payloads over 350 KB) |

Both tables are on-demand (`PAY_PER_REQUEST`). In AWS: point-in-time recovery, deletion protection and
encryption at rest on both tables; bucket versioning off (artifacts are write-once by
`If-None-Match: *`), Block Public Access on, SSE-S3 or SSE-KMS.

## 2. Why two tables (not one, not ten)

- **Not one table per Postgres table.** The owner ruled out a one-to-one conversion, and it would also
  be wrong: the change aggregate (request, attempts, approvals, parent, children) is read and written
  together under one optimistic lock, which is cheapest and strongly consistent when it is one item
  collection.
- **One application table** holds every entity the application owns. Transactions span entities
  (webhook delivery + request + job; registry change + audit event; usage record + its uniqueness
  guards), key prefixes keep entities apart, and one TTL attribute (`expires_at`) is set only on
  items meant to expire. Write volume is small (tens of writes per attempt), so isolation by table
  buys nothing.
- **A separate checkpoint table** because its schema belongs to a third-party library, which uses
  its own key prefixes (`CHECKPOINT_`, `CHUNK_`, `WRITES_`), and its data differs: high write volume
  (about 50 checkpoints and 120 writes per attempt), S3 offload, a TTL of its own, and LLM transcripts
  that call for a stricter IAM and retention policy. It can be emptied or rebuilt without touching
  domain data.

## 3. Conventions

- Primary key `PK` (S), sort key `SK` (S). Index keys `GSI1PK`/`GSI1SK`, `GSI2PK`/`GSI2SK`, `GSI3PK`/`GSI3SK`
  (S), set only on items that belong in the index (sparse indexes). Attribute `entity` names the type.
- UUIDs are lowercase canonical strings. Timestamps are UTC ISO-8601 with fixed microsecond width
  (`2026-11-27T09:29:17.162000Z`), so they sort as strings. `expires_at` is epoch seconds (N).
- Mutable entities carry `row_version` (N), incremented by every write. It replaces `SELECT ... FOR
  UPDATE` (section 5).
- JSON documents (profile `content`, parent `declaration`, audit `metadata`, job `payload`) are stored
  as **compact JSON strings**, not DynamoDB maps. A map would turn numbers into `Decimal` and could
  change the bytes behind `content_sha256`. Money (`cost_usd`) is a decimal string. Token counts are N.
- Time comes from an injected clock in the application (DynamoDB has no `now()`). This affects
  leases and report staleness. ECS hosts use Amazon Time Sync. Leases are 300 s, so clock skew of
  milliseconds does not matter.

## 4. Entity-to-key mapping (table `acca-app`)

`<rid>` request id, `<aid>` attempt id, `<pid>` parent id, `<cid>` child id, `<repo>` repository id,
`<vid>` profile version id, `<jid>` job id.

| Entity | PK | SK | GSIs | Notes / example |
|---|---|---|---|---|
| Jira delivery | `DELIVERY#<delivery_id>` | `DELIVERY` | - | `DELIVERY#5b2c...-webhook-17`. Written once with final `processing_result`, `request_id`; TTL `expires_at` (`ACCA_JIRA_DELIVERY_RETENTION_DAYS`, default 30 days) |
| Issue guard | `JIRAISSUE#<jira_issue_id>` | `GUARD` | - | `JIRAISSUE#10042` -> `request_id`. Unique Story |
| Code change request | `REQ#<rid>` | `REQUEST` | GSI1 `ISSUEKEY#<jira_issue_key>` / `REQ#<created_at>#<rid>` | Status projection, assignment, `row_version`. `status`: RECEIVED until the first attempt, then that attempt's status; BLOCKED when intake refused the request before any attempt |
| Attempt | `REQ#<rid>` | `ATT#<aid>` | GSI2 `LIST#ATTEMPT` / `<created_at>#<aid>` | Same attributes as today, plus `request_id` |
| Attempt pointer | `ATTEMPT#<aid>` | `PTR` | - | Immutable `request_id`, `number`. Cache forever in-process |
| Agent thread index | `ATTEMPT#<aid>` | `THREAD#<thread_id>` | - | `THREAD#attempt:<aid>:planning:9f3a1c2e` (studio only) |
| Active-attempt guard | `REQ#<rid>` | `ACTIVE#<repo>` | - | Exists while a non-terminal attempt exists for that repository (replaces the partial unique index) |
| Approval | `REQ#<rid>` | `APR#<aid>#<decided_at>#<approval_id>` | - | Append-only; optional `reason` (operator rejections only; absent = none, as on older items) |
| Multi-repo parent | `REQ#<rid>` | `PARENT` | - | One per request by construction; `declaration_json`, report lifecycle fields, `row_version` |
| Parent pointer | `PARENT#<pid>` | `PTR` | - | `request_id` |
| Multi-repo child | `REQ#<rid>` | `CHILD#<cid>` | - | `position`, `repository_id`, `depends_on` (string set or JSON list), status, ... |
| Child pointer | `CHILD#<cid>` | `PTR` | - | `request_id` |
| Child-attempt link | `CHILDATT#<aid>` | `PTR` | - | `request_id`, `child_id`; verified against `child.attempt_id` on read |
| Repository | `REPO#<repo>` | `REPO` | GSI2 `LIST#REPOSITORY` / `<repository_key>` | `row_version`, `last_profile_version` counter |
| Alias / key (global namespace) | `ALIAS#<alias>` | `ALIAS` | - | `kind` KEY or ALIAS, `repository_id` |
| Alias reverse copy | `REPO#<repo>` | `ALIAS#<alias>` | - | For `list_aliases` |
| Locator guard | `LOCATOR#<canonical_locator>` | `GUARD` | - | `LOCATOR#github:octo/service-a` (format as stored today) |
| Provider identity guard | `PROVIDERREPO#<git_provider>#<provider_repository_id>` | `GUARD` | - | `PROVIDERREPO#github#812345678` |
| Profile version | `REPO#<repo>` | `PV#<version:06d>` | - | `PV#000003`; `content_json`, `content_sha256`, `published_at` |
| Profile version pointer | `PROFILEVERSION#<vid>` | `PTR` | - | `repository_id`, `version` |
| Project mapping | `PROJECT#<jira_project_key>` | `MAP#<repo>` | GSI1 `REPOMAPS#<repo>` / `<jira_project_key>`; GSI2 `LIST#MAPPING` / `<project>#<created_at>#<mapping_id>` | The key is the unique pair |
| Registry audit event | `AUDIT#<entity_type>#<entity_id or ->` | `EV#<occurred_at>#<event_id>` | GSI2 `LIST#AUDIT` / `<occurred_at>#<event_id>` | Append-only |
| Usage, attempt copy | `USAGE#ATT#<aid>` | `<created_at>#<call_id>` | - | Immutable copy of the ledger row |
| Usage, session copy | `USAGE#SES#<session_id>` | `<created_at>#<call_id>` | - | Second immutable copy (both written in one transaction) |
| LLM call guard | `LLMCALL#<call_id>` | `GUARD` | - | Uniqueness of the call (run) id |
| Provider message guard | `LLMMSG#<provider_message_id>` | `GUARD` | - | Only when the provider reports an id |
| Job | `JOB#<jid>` | `JOB` | GSI3 `DISPATCH` (message not sent yet) or `SWEEP` (sent) / `<due_at>#<jid>` while non-terminal | State, attempts, lease, `payload_json`, `last_error` |
| Job dedupe guard | `JOBKEY#<dedupe_key>` | `GUARD` | - | `JOBKEY#code_change.child:<cid>:2` -> `job_id`. Permanent |

The change aggregate of one Story is a single item collection: `Query PK=REQ#<rid>` returns the request,
its attempts, approvals, active guards, parent and children (tens of items, one page).

## 5. Invariants: constraints and triggers mapped to conditions and transactions

| PostgreSQL mechanism (removed) | DynamoDB mechanism |
|---|---|
| `SELECT ... FOR UPDATE` (C2, C5, R3, R16, M9) | Optimistic lock: consistent read records `row_version`; the commit transaction conditions every item it depends on (`Update` with `row_version = :seen`, or `ConditionCheck`). On `TransactionCanceledException` with `ConditionalCheckFailed` or `TransactionConflict`, the service method is retried (bounded, jittered; section 5.1) |
| Unique `delivery_id`, `jira_issue_id`, `dedupe_key`, call id, `provider_message_id`, `canonical_locator`, `(git_provider, provider_repository_id)`, aliases and keys | A guard item keyed by the unique value, written with `attribute_not_exists(PK)` in the same transaction as the entity |
| Unique `(request_id, number)` | Numbers are allocated only in a transaction that conditions on the request's `row_version`; the attempt item is keyed by id |
| Partial unique: one non-terminal attempt per `(request, repository)` | `ACTIVE#<repo>` guard: `Put` with `attribute_not_exists` on create, `Delete` in the transaction that makes the attempt terminal |
| `attempt_guard` trigger: base commit immutable once set, terminal attempts immutable | Every attempt `Update` carries `NOT #status IN (:COMPLETED, :BLOCKED, :FAILED, :CANCELLED, :REJECTED) AND (attribute_not_exists(base_commit_sha) OR base_commit_sha = :new)` |
| `registry_profile_version_guard`: drafts may only be published, published versions immutable, no deletes | The only `Update` on a PV item is `SET published_at` with `attribute_not_exists(published_at)`; content attributes appear in no update expression |
| FK `repositories(id, active_profile_version_id)` -> a version of the same repository | `ConditionCheck` on the PV item (`repository_id = :repo AND attribute_exists(published_at)`) in the activation transaction |
| Check `ACTIVE requires active_profile_version_id` | `attribute_exists(active_profile_version_id)` on the status update |
| Append-only / never-delete triggers (approvals, audit, usage, attempts, parents, children, repositories) | No update or delete code path. The code deletes only the `ACTIVE#<repo>` guard (partition `REQ#<rid>`) and a superseded `CHILDATT#<aid>` link; production IAM allows `DeleteItem` only on partitions `REQ#*` and `CHILDATT#*` (condition `dynamodb:LeadingKeys`, which constrains the partition key only, so it cannot tell the guard from other items of the Story). `JOB#` items expire by TTL and the outbox is cleared by `UpdateItem`, so neither needs `DeleteItem`. Floci does not enforce IAM by default, so this is checked only in AWS |
| Check constraints (status enums, hex formats, ranges) | Domain models and Pydantic already validate; the adapter re-validates enums on read |
| Foreign keys (`ondelete=RESTRICT`) | Nothing is deleted, and writes are created by services that hold the parent |

### 5.1 Unit of work

`ChangeUnitOfWork` and `RegistryUnitOfWork` keep their ports. The DynamoDB implementations:

1. read immediately (consistent) and remember `row_version` of every item read through a `lock_*` or
   `get_*` call that a decision depends on;
2. buffer writes and serve **read-your-writes** from an in-memory overlay (`add_repository` then audit,
   `mark_published` then `set_active_version`, `_bump` returning the new row);
3. on `commit()`, send one `TransactWriteItems` (at most 100 items; the largest is the multi-repo create
   at 42) with a `ClientRequestToken`, so an SDK retry of the same commit is idempotent (verified on
   Floci, section 10.1);
4. map cancellation reasons by item index to the existing domain errors and codes
   (`ChangeConflictError`, `ConflictError(<code>)`), never exposing driver text, as the SQL adapters do.

New error `ConcurrentModificationError`, a subclass of the existing conflict errors, marks a lost
optimistic race. `CodeChangeService` and `RepositoryRegistryService` retry the whole method up to
5 times with jitter. This reproduces "wait for the row lock". Genuine conflicts (`alias_taken`,
`attempt_changed_concurrently` on a stale caller) are not retried.

## 6. Indexes

| Index | Keys | Projection | Items | Used by | Consistency note |
|---|---|---|---|---|---|
| GSI1 | `GSI1PK`, `GSI1SK` | KEYS_ONLY | Request (`ISSUEKEY#`), mapping (`REPOMAPS#`) | U3 issue -> request ids (from the index keys alone); R17 by repository (display: keys, then an eventual `BatchGetItem`) | Eventual. Requests exist long before a cost query; display only for mappings. Authorisation never uses a GSI |
| GSI2 | `GSI2PK`, `GSI2SK` | INCLUDE `entity_type`, `entity_id` | `LIST#REPOSITORY`, `LIST#MAPPING`, `LIST#AUDIT`, `LIST#ATTEMPT` | R5, R17 (no filter), R19 (filters on the two projected attributes), studio S1; keys, then an eventual `BatchGetItem` | Eventual; operator and studio listings |
| GSI3 | `GSI3PK`, `GSI3SK` | KEYS_ONLY | Non-terminal jobs: `DISPATCH` (outbox, not sent) or `SWEEP` (sent) / `<due_at>#<jid>` | Relay (`DISPATCH`) and sweep (`SWEEP`) (J8) | Eventual; each hit is re-checked with a consistent `GetItem` |

No LSIs (they would cap item collections at 10 GB and must be created with the table). No index projects
ALL: an index stores (and is written for) only what a listing needs to find its items, see section 13.

## 7. TTL, item size, hot partitions, pagination

**TTL.** Attribute `expires_at` on `acca-app`. Set on terminal job items (`SUCCEEDED`/`DEAD`;
retention 90 days) and on Jira delivery records (`ACCA_JIRA_DELIVERY_RETENTION_DAYS`, default 30
days: Jira redelivers within hours, and the permanent Story guard keeps a late redelivery from creating
anything). Never set on domain records, the usage ledger, the audit log or dedupe guards
(`JIRAISSUE#`, `JOBKEY#`, `LLMCALL#`, `LLMMSG#` stay forever). The checkpoint table uses the
library's `ttl` attribute (`ACCA_CHECKPOINT_TTL_DAYS`, default 30 days, owner cost request; the
saver installs the matching S3 lifecycle rule for offloaded payloads). TTL deletion is lazy, "within a few days" on AWS and about 60 s on Floci
(`dynamodb.md:151-155`). Correctness never depends on it.

**Item size (400 KB).** Enforced by Floci (probe: a 410 KB item was rejected with
`ValidationException: Item size has exceeded the maximum allowed size`). Measured maxima today: profile
content 593 bytes, parent declaration 1,302 bytes. Bounded maxima: a declaration of 20 repositories is
under 30 KB; usage rows are under 1 KB. The adapter refuses JSON documents over 300 KB with a domain
error instead of a raw `ValidationException`. Checkpoints (1.2 MB measured) go to `acca-checkpoints`
with S3 offload. Artifacts go to S3.

**Hot partitions.** Writes per partition per second stay far below 1,000 WCU and 3,000 RCU. Watch these:
`LIST#*` GSI2 partitions (one write per registry change or attempt creation; small), `DISPATCH` (one
write per enqueue and per terminal transition; heartbeats do not touch it), and a long multi-repo Story's
`REQ#<rid>` collection (child transitions are seconds apart). If the job rate ever reaches hundreds per
second, shard `DISPATCH#<0..7>`.

**Pagination.** Every `Query` loops on `LastEvaluatedKey`. Floci honours `Limit`, `ExclusiveStartKey`,
`ScanIndexForward` and `FilterExpression` on tables and GSIs (section 10.1). `Limit` counts items
before the filter, so filtered listings loop until the requested page is full. No `Scan` in
application code.

## 8. Job model: DynamoDB state, SQS delivery, transactional outbox

### 8.1 State machine (unchanged semantics)

`QUEUED -> RUNNING -> SUCCEEDED | QUEUED (retry with backoff) | DEAD`; `RUNNING` with an expired lease
is claimable again while `attempts < max_attempts`, otherwise it becomes `DEAD`
(`lease_expired_attempts_exhausted`). The `JobQueue` port (`application/jobs/queue.py:45-68`) and
`Worker` (`application/jobs/worker.py`) keep their contracts. The adapter keeps an in-process map
`job_id -> receipt handle`.

### 8.2 Flows

| Step | DynamoDB (authoritative) | SQS (delivery) |
|---|---|---|
| Enqueue (J1, W6) | `TransactWriteItems`: `JOBKEY#` guard + `JOB#` item (QUEUED, `GSI3` set = outbox row) | Worker-side producers send `{"job_id"}` right after commit and then move the GSI3 entry from `DISPATCH` to `SWEEP` (`dispatched_at` set; conditional on still being in `DISPATCH`). The API never sends (no SQS permission); the relay picks its jobs up |
| Relay (J8) | `Query GSI3 DISPATCH` for due rows, one consistent `BatchGetItem` re-read | `SendMessage`, then move the job from `DISPATCH` to `SWEEP` (the outbox entry is cleared). Runs as the dispatcher role inside the existing worker deployment (its own loop in the worker
process) every `JOB_RELAY_SECONDS` (default 30 s; only a fallback, producers send right after commit) |
| Claim (J2) | Conditional `UpdateItem` (same predicate as `_CLAIM`), `ALL_NEW` | `ReceiveMessage` (`WaitTimeSeconds` 20, `VisibilityTimeout` = lease, `MaxNumberOfMessages` 1 per free slot) |
| Not claimable | Read the item | Terminal -> `DeleteMessage`; not due -> `ChangeMessageVisibility(available_at - now)`; leased elsewhere -> visibility = remaining lease; exhausted and expired -> mark DEAD, `DeleteMessage` |
| Heartbeat (J4) | Conditional lease extension (owner, RUNNING, not expired). A condition failure means the lease is lost: the worker cancels the handler and records nothing. A failing call (outage, timeout, throttling) is logged (`job_heartbeat_failed`) and retried every beat while the last renewal still covers the next retry; after that the worker treats the lease as lost and stops before another worker can claim the job. Each call has its own timeout, ending a safety margin (min(5 s, lease/10)) before the last renewal runs out: a call that has not answered by then (`job_heartbeat_timeout`) is a lost lease too, so a hung call can never outlive the lease. Only that deadline's expiry counts: a `TimeoutError` raised by the call itself is an ordinary failed call. The renewal uses its own clients (`AwsClients.dynamodb_heartbeat` / `sqs_heartbeat`, `heartbeat_config`: connect 2 s + read 3 s, one botocore attempt, no table-level retry), so a call the worker abandoned at the deadline, which keeps running in its thread, ends within about one margin. Residual: such a call can still succeed late; it then extends only this worker's own lease (the condition names this owner, so it can never take over a job another worker claimed) and the job is redelivered after that lease, never run twice at once | `ChangeMessageVisibility(lease)`; failure is non-fatal (DynamoDB decides) |
| Complete (J5) | Conditional SUCCEEDED, `expires_at` set | `DeleteMessage` |
| Fail, retry (J6) | Conditional QUEUED, `available_at = now + retry` (le 900 s) | `ChangeMessageVisibility(retry)` on the same message: no new message, no outbox row |
| Fail, dead | Conditional DEAD | `DeleteMessage` |
| Worker crash | Lease expires | Visibility expires with the lease; the next receiver's conditional claim succeeds (`attempts + 1`), as today |
| Lost or dead-lettered message for a live job | Sweep (every `JOB_SWEEP_SECONDS`, default 300 s, at least the relay interval) queries GSI3 `SWEEP` (non-terminal, sent) and, after one consistent `BatchGetItem` re-read, picks jobs whose `available_at` (QUEUED) or lease (RUNNING) passed by more than a grace period (default 300 s) and that were not re-sent within it | Re-send, `dispatched_at` = now. Terminal transitions `REMOVE` the GSI3 keys |

### 8.3 Outbox evaluated

| Option | Atomic with the domain write | Moving parts | Verdict |
|---|---|---|---|
| Direct `SendMessage` after commit | No: a crash between commit and send leaves a QUEUED job with no message, and the API would need SQS permissions | None | Rejected on its own; kept as the fast path for worker-side producers |
| DynamoDB Streams -> Lambda or EventBridge Pipes -> SQS | Yes | Streams, Lambda or Pipe, IAM; on Floci, stream records are in memory and lost on restart (`dynamodb.md:145-147`); no per-message delay for backoff | Rejected: heavier, and it does not help retries |
| **Job item as outbox row + sparse GSI3 + in-worker relay** | Yes (same `TransactWriteItems`) | One sparse GSI, one loop in the existing worker | **Recommended**. It also acts as the reconciler for lost messages |

### 8.4 Duplicate safety

1. **Dedupe** is a permanent guard item (`JOBKEY#`), so one job per key, as with today's unique index.
2. **At-least-once delivery**: every receipt goes through the conditional claim, so a duplicate message,
   a relay re-send or a late redelivery after completion is a no-op followed by `DeleteMessage`.
3. **Crash**: the lease, not the message, decides ownership. Handlers stay idempotent (they already
   are: `WorkflowRunner.start` resumes, the coordinator uses CAS, Jira reporting reconciles).
4. **Poison**: `maxReceiveCount` 20 moves a message that keeps failing before any state change (bad body,
   unknown job) to `acca-jobs-dlq`. The DLQ is alarmed, and DynamoDB still says what happened. Not-due
   reschedules also count as receives, hence 20 and not `max_attempts`. Operators redrive with
   `StartMessageMoveTask` (verified in S2115).
5. **Standard, not FIFO**: FIFO's 5-minute deduplication window cannot replace permanent dedupe, and no
   handler needs ordering. Claim order becomes best-effort instead of `ORDER BY available_at`.
6. **Limits**: `DelaySeconds` le 900 and visibility le 43,200 s (AWS). The 900 s backoff cap
   (`worker.py:26`) fits exactly; `job_lease_seconds` le 3,600 (`core/config.py:186`). Settings must keep
   lease le 43,200 because Floci does not enforce it (section 10.2).

## 9. LangGraph checkpointer

### 9.1 Options

| Option | Status | Verdict |
|---|---|---|
| `langgraph_checkpoint_aws.DynamoDBSaver` (`langgraph-checkpoint-aws` 1.3.0, released 2026-11-19 from `langchain-ai/langchain-aws` `libs/langgraph-checkpoint-aws`, tag `langgraph-checkpoint-aws==1.3.0`) | Requires `langgraph-checkpoint>=3.0.0,<5.0.0`, `langgraph>=1.0.0`, `boto3>=1.43.64`, Python 3.11 to 3.13 (PyPI metadata). The locked versions fit: langgraph 1.2.24, langgraph-checkpoint 4.3.2, boto3 1.44.11 | **Recommended** |
| Custom DynamoDB saver | About 1,000 lines (the library's `unified_repository.py` is 1,094) plus conformance work | Rejected: duplicates a maintained, conformance-tested library |
| S3-only saver | No ordered listing, no pending writes; `aget_state` and resume need both | Rejected |
| `AgentCoreMemorySaver` (same package) | Bedrock AgentCore Memory service | Rejected: ties checkpoints to a Bedrock service, which conflicts with the anthropic and Floci modes |
| `ValkeySaver` (same package) | Redis-compatible | Forbidden (no Redis) |

### 9.2 DynamoDBSaver facts (source at tag 1.3.0)

- Table schema: `PK`/`SK` strings; items `CHECKPOINT_<thread_id>` / `<ns>#<checkpoint_id>`, payloads under
  `CHUNK_...`, writes under `WRITES_...` (`checkpoint/dynamodb/README.md`, "Unified Table Design").
- Payloads larger than `S3_OFFLOAD_THRESHOLD = 350 * 1024` (358,400 bytes, a module constant in
  `checkpoint/dynamodb/storage_strategy.py:24`, not configurable) go to S3 when `s3_offload_config` is set;
  otherwise they stay in DynamoDB and fail at the 400 KB item limit. **S3 offload is mandatory here**:
  the largest blob already stored is 1.2 MB.
- Threshold safety, accounting for item metadata: each payload is written as its own chunk item
  (`PK` = `CHUNK_...` string, `SK` = `CHUNK`, `payload` binary, optional `ttl` number;
  `storage_strategy.py:247-256`); the checkpoint's base item holds only keys and references. The
  DynamoDB item limit is 409,600 bytes including attribute names, so a chunk item at the threshold is
  358,400 bytes of payload plus under 1 KB of names, keys and TTL (thread ids here are under 120
  characters), leaving about 50 KB of headroom. The application keeps thread ids under 200 characters
  and tests a payload at exactly 358,400 bytes (stays in DynamoDB), one byte over (goes to S3), and
  states of 600 KB and 1.3 MB (offloaded, round-tripped, deleted with the thread).
- `consistent_read` (keyword, default `False`): "Enable this when a different worker or process may
  resume a thread immediately after a checkpoint is written". This applies to the resume job after
  an approval, so `True`.
- The async methods are `run_in_executor` wrappers around boto3 (`saver.py:232-242, 296-319, 357-377,
  466-494, 519-529`): thread-pool I/O, fine at worker concurrency 2 to 32.
- Implements `get_tuple`, `put`, `put_writes`, `list`, `delete_thread`, but not `delete_for_runs`,
  `copy_thread` or `prune`. The application uses none of those.
- `ttl_seconds` sets DynamoDB TTL and an S3 lifecycle rule (day precision). Optional gzip compression.
- Accepts a `session` and `boto_config`, so the application's one AWS client factory can own
  credentials and endpoints.

### 9.3 Verified on Floci (2026-11-27, scratch venv, no project files changed)

| Check | Result |
|---|---|
| Official `langgraph-checkpoint-conformance` 0.0.2 suite, `DynamoDBSaver` default and `consistent_read=True` | **Both FULL (5/5 base capabilities)**: put, put_writes, get_tuple, list (incl. metadata filters, before, limit), delete_thread; 2 passed in 2.3 s. Extended: copy_thread, delete_for_runs, prune "not implemented" |
| Graph with `interrupt()` gate: `ainvoke` on saver instance A, `aget_state` + `Command(resume=True)` on a new instance B | Interrupt `{"awaiting": "plan_approval"}` seen by B, resume completed, 5 checkpoints, `adelete_thread` left 0 |
| 600 KB state, no S3 offload | Failed: `ValidationException ... Item size has exceeded the maximum allowed size` |
| 600 KB state, S3 offload to a Floci bucket | Passed (interrupt, resume, 600,000-byte value intact); `adelete_thread` also removed the S3 objects (0 left) |

### 9.4 Configuration (approved)

`DynamoDBSaver(table_name=<checkpoint table>, session=<app session>, boto_config=<app config>,
consistent_read=True, s3_offload_config={"bucket_name": <bucket>, "endpoint_url": <local only>},
ttl_seconds=ACCA_CHECKPOINT_TTL_DAYS * 86_400)` (default 30 days, 1..365; was 90 days), pinned `langgraph-checkpoint-aws==1.3.0` (exact: 15 releases since
1.0.4). Add the conformance suite to the `floci` tests so an upgrade is gated.

The saver takes no `serde` argument, so `use_checkpoint_serde` (`checkpointer.py`) replaces both
`saver.serde` and `saver.serializer.serde` (the copy 1.3.0 takes at construction) with a
`JsonPlusSerializer(allowed_msgpack_modules=...)` that allows LangGraph's safe types plus
`ImplementationPlan`, `ImplementationReport` and `PlannedChange`. Any other type is blocked and read
back as raw data, so a new type stored in graph state must be added to `CHECKPOINT_MSGPACK_TYPES`.

## 10. Floci 2.5.0 verification

Probes: `scratchpad/s2116/probes/{ddb_probe.py, sqs_s3_probe.py, bedrock_probe.py, graph_probe.py,
test_conformance_floci.py}` (session scratchpad), boto3 1.44.11, `test`/`test` credentials,
`AWS_PROFILE` unset, config and credential files `/dev/null`, endpoint `http://127.0.0.1:4566`. All probe
tables, queues and buckets (`s2116probe-*`) were deleted afterwards.

### 10.1 DynamoDB (`docs/services/dynamodb.md` at 2.5.0)

| Feature | Docs | Live probe |
|---|---|---|
| `TransactWriteItems` / `TransactGetItems` | Listed, "ACID write transaction" (`dynamodb.md:41-42`) | Failing condition on item 3 cancelled all 3 (`TransactionCanceledException`, reasons `[None, None, ConditionalCheckFailed]`, nothing written). `ConditionCheck` + `Update` + `Put` committed. Two operations on one item rejected (`ValidationException`), as on AWS. 100 items accepted, 101 rejected. `ClientRequestToken`: replay applied once; same token, other body -> `IdempotentParameterMismatchException` |
| `ConditionExpression` | Expression and legacy conditions (`dynamodb.md:105-128`) | `attribute_not_exists` duplicate -> `ConditionalCheckFailedException`; `ReturnValuesOnConditionCheckFailure=ALL_OLD` returned the existing item. 12 threads racing a `row_version` CAS: exactly 1 winner |
| Concurrent transactions | - | 12 threads, one item: 1 winner, others `TransactionCanceledException` with `ConditionalCheckFailed`. **`TransactionConflict` never appeared** (Floci serialises) |
| GSIs | Supported; deviations: no index stored, index reads immediately consistent, new index ACTIVE at once (`dynamodb.md:359-384`) | Table + GSI ACTIVE immediately; GSI query sorted by range key; pagination over GSI (`Limit` 8 -> 4 pages, 30 items); sparse GSI excluded an item without keys; `ConsistentRead` on a GSI rejected (`ValidationException`), as on AWS |
| TTL | Sweep every 60 s; expired items visible until swept; filter on the attribute (`dynamodb.md:149-155`) | `UpdateTimeToLive` ENABLED; an expired item was still readable, then deleted after 55 s; future item kept; TTL delete emitted a `REMOVE` stream record with the service identity |
| `Query` pagination | `Query` listed (`dynamodb.md:36`) | 30 items, `Limit` 7 -> 5 pages; `ScanIndexForward=false` reversed; `FilterExpression` returned 15 of 30 over 5 pages; `begins_with` key condition |
| Streams | `DynamoDBStreams_20120810`: ListStreams, DescribeStream, GetShardIterator, GetRecords; records in memory (`dynamodb.md:130-147`) | `NEW_AND_OLD_IMAGES`, 1 shard, INSERT/MODIFY/REMOVE records read from TRIM_HORIZON. Not used by this design |
| Item size | - | 380 KB accepted; 410 KB rejected |
| `BatchWriteItem` | "up to 25 items" (`dynamodb.md:39`) | 25 accepted, 26 rejected |
| Atomic counter | - | `ADD ... ReturnValues=UPDATED_NEW` returned 1, 2, 3 |
| Storage | `single-file` layout rewrites every table on any write; `per-table` + `wal` recommended for write-heavy use (`dynamodb.md:281-303`) | Not probed; heartbeats and checkpoints are write-heavy, so set `FLOCI_STORAGE_SERVICES_DYNAMODB_ITEM_LAYOUT=per-table` and `FLOCI_STORAGE_SERVICES_DYNAMODB_MODE=wal` in `infra/docker-compose.yml` |

### 10.2 SQS and S3 (used by the job model and artifact store)

| Feature | Docs | Live probe |
|---|---|---|
| `ChangeMessageVisibility` | Listed (`sqs.md:21-22`) | Extending hid the message; 0 made it visible at once; `ApproximateReceiveCount` 2 |
| `DelaySeconds` | 0 to 900, others rejected (`sqs.md:106`) | Delayed 3 s as asked; 901 rejected |
| Redrive | `RedrivePolicy` (`sqs.md:195`; verified in S2115) | Moved to the DLQ after `maxReceiveCount` 3 |
| Long polling | 0 to 20 s (`sqs.md:83-96`) | Used throughout |
| S3 `If-None-Match: *` | Not documented (`s3.md` mentions only `If-Range`, line 210) | **Works**: second put -> `PreconditionFailed`, original body kept. User metadata and prefix listing work |

### 10.3 Bedrock Runtime (`docs/services/bedrock-runtime.md` at 2.5.0)

Backends: `stub` (default, fixed reply) and `proxy` ("forwards `Converse` and `ConverseStream` requests to
any OpenAI-compatible `/chat/completions` endpoint (Ollama, OpenRouter, LiteLLM, vLLM, ...), translating
request/response shapes including tool use", `bedrock-runtime.md:8-11`). Configuration: `..._BACKEND`,
`..._PROXY_URL`, `..._PROXY_API_KEY` (sent as `Authorization: Bearer`), `..._PROXY_MODEL_MAPPING`,
`..._PROXY_PASSTHROUGH` (`bedrock-runtime.md:32-41`); resolution order mapping, passthrough, default model,
else 400 (`bedrock-runtime.md:45`).

Probes: the `stub` backend on the shared `acca-floci`. The `proxy` backend on a **separate, throwaway**
Floci 2.5.0 container (same pinned digest, `127.0.0.1:4577`, no Docker socket, removed afterwards)
pointed at a local fake OpenAI-compatible server (no model; it logged each upstream request).
Clients: boto3 `bedrock-runtime` and `langchain_aws.ChatBedrockConverse` 1.10.0 with `endpoint_url`.

| Capability | `stub` | `proxy` (fake upstream) |
|---|---|---|
| `Converse` text | Fixed text, usage 10/12 | Translated; usage 11/7 mapped from `prompt_tokens`/`completion_tokens` |
| `Converse` tool use (`toolConfig`) | **Ignored: never returns `toolUse`** (docs: "tool-use round-tripping is not implemented", `bedrock-runtime.md:26`) | `toolUse` block, `stopReason tool_use`; `toolResult` round trip became an OpenAI `tool` message with `tool_call_id`; system prompt sent as a `system` message; `max_tokens` forwarded |
| `ConverseStream` | Event-stream framed: messageStart, contentBlockDelta, contentBlockStop, messageStop, metadata | Same events; upstream got `stream: true`, `stream_options.include_usage: true`; a streamed tool call arrived as one `toolUse` start (accumulated, `bedrock-runtime.md:190`), `stopReason tool_use` |
| `cachePoint` blocks (prompt caching) | Accepted | **Accepted and dropped** before the upstream request: no prompt caching, cache token counts always 0 |
| `InvokeModel` | Anthropic-shaped stub body | **`ValidationException: InvokeModel is not supported by the bedrock-runtime proxy backend; use Converse.`** (the docs, `bedrock-runtime.md:188`, still say it falls back to the stub) |
| `InvokeModelWithResponseStream` | 501 `UnsupportedOperationException` (`bedrock-runtime.md:22,189`) | Same |
| `ChatBedrockConverse.ainvoke` / `bind_tools` / `astream` | Works; no tool calls | Works: `tool_calls=["read_file"]`, tool round trip, streamed `usage_metadata` (input 11, output 7) |
| Management plane, Guardrails, Agents, provisioned throughput | Out of scope (`bedrock-runtime.md:13,191-192`) | Same |

The application calls only `ChatBedrockConverse` (Converse and ConverseStream), so the proxy covers it.
The stub cannot drive a Deep Agent, which needs tool calls.

### 10.4 Gaps to design and test around

1. GSIs are immediately consistent on Floci, eventually consistent on AWS. No decision path reads a
   GSI (sections 4 and 6). The in-memory test fake of the table adds an optional GSI lag.
2. `TransactionConflict` and throttling never occur on Floci. Unit tests with botocore `Stubber` cover
   retry on `TransactionConflict`, `ThrottlingException` and `ProvisionedThroughputExceededException`.
3. Floci accepted `ChangeMessageVisibility` with a stale receipt handle and a timeout of 43,201 s; AWS
   rejects both. Visibility failures are non-fatal in the design; Settings cap the lease.
4. TTL is swept every 60 s on Floci but lazily on AWS; tests must not wait for TTL deletion as proof of
   behaviour.
5. The current floci.io environment page lists `FLOCI_SERVICES_DYNAMODB_TRANSACTION_CONFLICTS_ENABLED`, but the
   2.5.0 reference does not, so it is not used with the pinned image; `Stubber` tests cover conflicts.
6. IAM is not enforced by default; least-privilege policies are reviewed, not tested locally, unless
   a test enables `FLOCI_SERVICES_IAM_ENFORCEMENT_ENABLED`.
7. GSI queries read the base table on Floci (`dynamodb.md:379`), so local latency is not representative.
8. Bedrock: no prompt caching or cache-token accounting through the proxy; `InvokeModel` unsupported on
   the proxy (contradicting the docs); stub has no tool use.
9. Persistence default (`single-file`) rewrites all tables on each write (`dynamodb.md:293`).

## 11. LLM modes

| Mode | Settings | Path | Where allowed |
|---|---|---|---|
| `anthropic` | `LLM_PROVIDER=anthropic` | `ChatAnthropic` (unchanged) | development, test (Settings already refuse it in staging/production, `core/config.py:311-313`) |
| bedrock via Floci | `LLM_PROVIDER=bedrock`, `ACCA_BEDROCK_ENDPOINT_URL=http://127.0.0.1:4566` from the host or `http://acca-floci:4566` from a container on the `acca-floci` network (new; development/test only; loopback or a single-label Docker hostname only) | `ChatBedrockConverse(endpoint_url=...)` -> Floci `proxy` -> OpenAI-compatible upstream | local |
| real Bedrock | `LLM_PROVIDER=bedrock`, no endpoint override | `ChatBedrockConverse` -> AWS Bedrock Runtime | staging, production |

Upstream for the Floci proxy, without LiteLLM: Anthropic's OpenAI SDK compatibility endpoint
`https://api.anthropic.com/v1/` accepts `Authorization` bearer keys, `tools`, `stream` and
`stream_options` ("Fully supported"), but "Prompt caching is not supported" and the layer "is not
considered a long-term or production-ready solution for most use cases"
(platform.claude.com/docs/en/api/openai-sdk). That is acceptable for a local-only mode; Floci needs
`..._PROXY_MODEL_MAPPING` from Bedrock model ids to Claude API ids. LiteLLM (latest stable
`v1.111.0`, image `ghcr.io/berriai/litellm:v1.111.0`, cosign-signed, config-file mode without a
database) is the fallback if the compatibility layer is not good enough. It adds a container that also
holds the key. Running the real upstream is a real model call and needs explicit owner authorisation.

Cost ledger in the Floci mode: usage comes back as input/output only, with no cache tokens, and is
priced by the Bedrock model id. Record `provider = "bedrock-floci"` so these rows are not mistaken
for real Bedrock spend.

## 12. What moves to S3 (and what does not)

| Data | Today | After |
|---|---|---|
| Attempt artifacts (plans, manifests, implementation reports, delivery results, cross-repo contracts) | `LocalFilesystemArtifactStore` under `ARTIFACT_ROOT` (`execution/artifacts.py:103-241`), behind `ArtifactStore` (`application/artifacts/ports.py:14-39`) | `S3ArtifactStore`: `artifacts/<request_id>/<attempt_id>/<artifact_id>.blob` plus `.meta.json`; `put` with `IfNoneMatch="*"` (never overwrites; verified on Floci); `get` re-hashes; `list` by prefix; size limits unchanged |
| LangGraph checkpoint payloads 350 KB or larger | `langgraph.checkpoint_blobs` (up to 1.2 MB) | `checkpoints/` prefix through `DynamoDBSaver` S3 offload |
| Dependency bundles (`ARTIFACT_ROOT/dependency-bundles`, `composition.py:266-270`) | Local | **Stay local**: they are bind-mounted into sandboxes |
| Workspaces (`WORKSPACE_ROOT`) | Local | **Stay local** (Docker sandbox) |
| JSON documents in domain items (declaration, profile content, audit metadata) | JSONB | DynamoDB string attributes (all far below 400 KB) |
| Studio PR link (`studio/history.py:148-166` reads the artifact folder) | Filesystem | Through the `ArtifactStore` port |

## 13. Cost

Prices change and differ by region, so this section reasons in request units and bytes only. The
unit rules and the current prices are on the AWS pricing page (aws.amazon.com/dynamodb/pricing, "on-demand
capacity"): one read request unit (RRU) is a strongly consistent read of up to 4 KB, an eventually
consistent read is half an RRU, a transactional read two; one write request unit (WRU) is a write of up
to 1 KB, a transactional write two. A `Query` is charged on the summed size of the items it reads
(rounded up to 4 KB once), `GetItem` and every `BatchGetItem` item on its own size (each rounded up to
4 KB). `ProjectionExpression` and `FilterExpression` reduce what comes back, never what is charged.
Every write that adds, changes or removes an index entry is also charged as an index write; a write that
touches no projected attribute and no index key writes no index. Storage is charged per GB-month,
indexes included. On-demand has no per-table or per-index fixed charge.

**Billing mode: on-demand (`PAY_PER_REQUEST`) for both tables.** Traffic is bursty and low (a Story is
tens of writes spread over minutes to hours; idle most of the day), so paying per request beats paying
for provisioned capacity by the hour. Provisioned capacity with auto-scaling pays off only for steady,
predictable traffic: compare `RRU/month * price_rru + WRU/month * price_wru` (on-demand) with
`(RCU + WCU provisioned at the auto-scaling target utilisation) * hours * hourly price`; revisit for the
production milestone once Task 18's per-Story unit counts and real Story volume are known.

**Table class: `STANDARD` for both tables** (`TableClass` in `infra/floci/schema/*.json`). Both tables
are request-dominated (the checkpoint table is written on every graph step; the app table is read and
written on every transition). `STANDARD_INFREQUENT_ACCESS` lowers the storage price and raises the
request price, so it pays off only when storage is the larger part of a table's bill (large, rarely
read data); the app table's ledger and audit history could reach that after years, the checkpoint table
never will with a 30-day TTL. The class can be switched with `UpdateTable` (AWS limits how often).

**Point-in-time recovery and deletion protection** are production choices, not enabled locally.
Deletion protection has no charge and should be on for both production tables. Point-in-time recovery is
charged per GB-month of table size: worth it for `acca-app` (the only copy of requests, registry,
ledger and audit history), optional for `acca-checkpoints` (30-day TTL; re-creatable only for finished attempts: an attempt paused at an approval gate cannot resume without its checkpoint and fails with `checkpoint_expired`).

**Indexes: three, each serving real queries.** GSI1: U3 (cost report by Story: issue key -> request ids)
and R17 by repository. GSI2: studio S1 (recent attempts), R5 (repositories), R17 (all mappings), R19
(audit, optionally filtered). GSI3: the outbox relay and the sweep (J8). Merging GSI2 into GSI1 would not
save money: on-demand has no per-index charge, and the same items would be written to the merged index.
Projections are minimal (section 6):

| Index | Projection | Why | Effect |
|---|---|---|---|
| GSI1 | KEYS_ONLY | U3 needs only the request id (from `PK`); R17 re-reads the mappings | A request's status projection changes on every attempt transition: no GSI1 write any more (at least 1 WRU saved per transition) |
| GSI2 | INCLUDE `entity_type`, `entity_id` | Listings fetch their items with one eventual `BatchGetItem`; R19 filters on the two attributes inside the index | An attempt changes on every transition: no GSI2 write any more (at least 1 WRU per transition); audit events are stored in the index as keys plus two short attributes instead of their metadata |
| GSI3 | KEYS_ONLY | The relay and the sweep re-read every hit consistently anyway | Heartbeats, claims and lease changes never write GSI3; only the DISPATCH -> SWEEP move and the terminal REMOVE do |

The price is paid on listings: a studio page of 50 attempts reads about 1.5 RRU of index keys plus
25 RRU (50 eventual `BatchGetItem` reads) instead of about 3.5 RRU from an ALL index. A Story's attempt
makes 10-20 transitions, each saving at least 2 index writes, so the trade pays off unless the attempt
list is opened many times per attempt (the studio lists on demand; it never polls the list).

**Sparse indexes.** Only these items carry index keys: GSI1 `code_change_request`, `project_mapping`;
GSI2 `attempt`, `repository`, `project_mapping`, `audit_event`; GSI3 `job` while not terminal (the
terminal transition removes the keys). Guards, pointers, deliveries, aliases, profile versions,
approvals, threads, parents, children and usage rows carry none (`tests/unit/persistence_dynamodb/
test_table_schema.py`).

**Reads.** Decision paths (claims, state transitions, dedupe, approvals, authorisation:
project -> repository mapping and repository status) read strongly consistent and are never cached.
Display-only paths read eventually consistent (half price): the studio attempt list, R5, R17 without a
project, R19 (both the GSI2 listing and the entity partition). The cost report (U2-U4) stays consistent
because the parent's Jira report is built from it. Hot-path request counts are pinned by
`tests/integration/floci/test_dynamo_call_counts.py`: webhook intake 1 `TransactWriteItems`; job claim
1 `ReceiveMessage` + 1 `UpdateItem`; heartbeat and completion 1 `UpdateItem` each; an attempt
transition 3 reads (attempt, approvals, request) + 1 `TransactWriteItems`; an idle relay or sweep tick
1 `Query`. Writes whose result is not used ask for `ReturnValues=NONE` (no charge difference: it saves
the response bytes). The relay and the sweep re-read their hits with one `BatchGetItem` per 100 jobs
(same units as one `GetItem` each, far fewer requests).

**Polling defaults and idle cost.** Per worker, with no work: the relay (`JOB_RELAY_SECONDS`, default
30 s) runs 2,880 GSI3 Queries a day, the sweep (`JOB_SWEEP_SECONDS`, default 300 s) 288; an empty
eventually consistent Query is charged the 0.5 RRU minimum, so about 1,584 RRU per worker per day
(before: 2 s and 60 s, about 22,320 RRU a day). Each sweep also reads every live job (1 RRU each,
consistent). The relay is only a fallback: worker-side producers send right after commit, so a lower
relay rate delays only intake jobs written by the API (up to one relay interval) and jobs whose direct
send failed. SQS long polling (`WaitTimeSeconds` 20) bounds idle `ReceiveMessage` calls per claim slot
to about 4,320 a day (SQS is priced per request; see its pricing page).

**TTLs** (section 7): terminal jobs 90 days, Jira delivery records 30 days, checkpoints 30 days (DynamoDB
TTL plus the S3 lifecycle rule on offloaded payloads). TTL deletes are not charged as writes. Audit
events, the usage ledger and every dedupe guard are permanent.

**Item size and S3 offload.** Hot items stay within one write unit: measured on Floci (largest of each
in `test_dynamo_call_counts.py`) `code_change_request` 526 bytes, `attempt` 551, `job` 557,
`jira_delivery` 420. Large text never sits in hot items: artifacts (plans, manifests, reports) are in
S3 and attempts hold only a reference and a hash; checkpoint payloads over the library threshold
(358,400 bytes) go to S3. The largest JSON documents (parent declaration, profile content, audit
metadata) are read only by their own operations. Attribute names stay readable.
