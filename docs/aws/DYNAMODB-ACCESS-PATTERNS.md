# DynamoDB access patterns (derived from code)

Status: approved design input (owner, 2026-11-27). Read-only analysis of the PostgreSQL implementation
at commit `26ce3fd`; every file:line reference points at that commit. DynamoDB starts empty: no
record is migrated, there is no dual-write period and no backend flag. The SQL adapters, models and
Alembic migrations cited below are deleted when DynamoDB becomes the only database (ADR-015); git
history keeps them. Every row comes from a repository method in
`src/ai_code_change_agent/persistence` or from a direct SQL caller (studio). Paths below are relative
to `src/ai_code_change_agent/` unless they start with `studio/` or `tests/`. Key names refer to
[DYNAMODB-DATA-MODEL.md](DYNAMODB-DATA-MODEL.md).

Legend:

- **Frequency**: per Story (one Jira delivery), per attempt, per LLM call, per job, per heartbeat, or
  operator (CLI, rare). Measured volume in the local database on 2026-11-27 (sizing only; none of it is migrated): 5 deliveries,
  5 requests, 19 attempts, 19 approvals, 25 jobs, 4 repositories, 8 profile versions, 4 mappings,
  36 audit events, 339 usage rows (about 18 per attempt), 2 parents, 5 children, 948 LangGraph
  checkpoints with a largest blob of 1,246,727 bytes.
- **Consistency**: `strong` means the decision that follows must see the latest committed write
  (base-table `GetItem`/`Query` with `ConsistentRead=true`, or a write condition).
  `eventual` means display-only or tolerant, so a GSI is acceptable.
- **Tx**: whether the current code relies on a multi-row transaction or a row lock.

## 1. Webhook intake and dedupe

Adapter: `persistence/repositories/webhook_repository.py` (`SqlAlchemyWebhookRepository`). Caller:
`application/webhooks/receive.py:85` (`ReceiveJiraWebhook`), mounted by `api/routes/jira_webhook.py`,
built in `composition.py:102-118`.

| # | Operation (file:line) | Current SQL / op | Purpose | Frequency | Consistency | Uniqueness | Tx | Proposed DynamoDB |
|---|---|---|---|---|---|---|---|---|
| W1 | `record` 34-41 | One SQLAlchemy transaction around W2-W6; any exception becomes `PersistenceError(class name)` | Atomic intake | per delivery | strong | - | **yes, all of W2-W6** | One `TransactWriteItems` (accepted path) or one conditional `PutItem` (ignored path); `TransactionCanceledException` reasons decide the outcome |
| W2 | `_record` 47-69 | `INSERT jira_deliveries ... ON CONFLICT (delivery_id) DO NOTHING RETURNING id` | Dedupe redelivered webhooks (`Outcome.DUPLICATE`) | per delivery | strong | `delivery_id` unique (`models/webhooks.py:55`) | yes | `Put DELIVERY#<delivery_id>/DELIVERY` with `attribute_not_exists(PK)`; cancellation reason at index 0 = duplicate delivery |
| W3 | `_record` 73-86 | `INSERT code_change_requests ... ON CONFLICT (jira_issue_id) DO NOTHING RETURNING id` | One request per Jira Story | per accepted delivery | strong | `jira_issue_id` unique (`models/webhooks.py:25`) | yes | Guard `Put JIRAISSUE#<jira_issue_id>/GUARD` (`attribute_not_exists`, `ReturnValuesOnConditionCheckFailure=ALL_OLD`) plus `Put REQ#<rid>/REQUEST` in the same transaction |
| W4 | `_record` 87-94 | `SELECT id FROM code_change_requests WHERE jira_issue_id = ?` | Find the existing request for a second delivery of the same Story | rare | strong | - | yes | Read `request_id` from the guard's `ALL_OLD` image in the cancellation (no extra read), else consistent `GetItem JIRAISSUE#<id>` |
| W5 | `_record` 95-100 | `UPDATE jira_deliveries SET processing_result='DUPLICATE_ISSUE', code_change_request_id=?` | Link the duplicate delivery to the existing request | rare | strong | - | yes | Second conditional `PutItem DELIVERY#...` written with `DUPLICATE_ISSUE` and the existing `request_id` (values computed before the write; no update needed) |
| W6 | `_enqueue_intake` 110-127, `_record` 102-107 | `INSERT jobs ... ON CONFLICT (dedupe_key) DO NOTHING`; `UPDATE jira_deliveries SET code_change_request_id` | Intake job created atomically with the request (ADR C1) | per accepted delivery | strong | `jobs.dedupe_key` unique (`models/jobs.py:40`) | **yes** | Same transaction: `Put JOBKEY#code_change.intake:<rid>/GUARD`, `Put JOB#<jid>/JOB` (status QUEUED, dispatch pending = transactional outbox). Delivery item written once with `request_id` already set |
| W7 | index `ix_jira_deliveries_code_change_request_id` (`models/webhooks.py:51`) | B-tree index | None: no code reads deliveries by request | - | - | - | - | No GSI. `request_id` stays an attribute |

Ignored deliveries (`ProcessingStatus.IGNORED`, `webhook_repository.py:70-71`) need only W2.

## 2. Code change requests, attempts and approvals

Adapter: `persistence/repositories/change_store.py` (`SqlChangeStore`, `SqlChangeUnitOfWork` 191-213:
one session per unit of work, rollback on exit unless committed). Port:
`application/changes/ports.py:14-41`. Service: `application/changes/service.py` ("each method is one
transaction: the attempt row is locked", lines 1-5). Callers: `orchestration/steps.py` (28 service
calls per workflow run), `orchestration/intake.py:76,126,129,165,170,180`,
`application/multirepo/coordinator.py:311,489`, `cli/operations.py:127-128,146`, cost status lookup
`composition.py:405-407`.

| # | Operation (file:line) | Current SQL / op | Purpose | Frequency | Consistency | Uniqueness | Tx | Proposed DynamoDB |
|---|---|---|---|---|---|---|---|---|
| C1 | `get_request` 87-89 | `session.get(code_change_requests, id)` | Read a request (intake, report, child context) | per job, several | strong | PK | no | `GetItem REQ#<rid>/REQUEST`, `ConsistentRead` |
| C2 | `lock_request` 91-97 | `SELECT ... FOR UPDATE` | Serialise assign/start/project (service 58, 87, 114, 146, 212) | per attempt mutation | strong | - | **row lock** | Consistent `GetItem`; the unit of work remembers `row_version` and every write it commits carries `row_version = :seen` (optimistic lock). Conflict = retry the service method |
| C3 | `update_request` 99-110 | `UPDATE code_change_requests SET repository_id, profile_version_id, current_attempt_id, status` | Assignment and status projection (service 80, 158-169) | per attempt mutation | strong | - | yes (with C2) | `Update REQ#<rid>/REQUEST` in the commit transaction, `SET ... , row_version = row_version + 1`, condition on the seen version |
| C4 | `get_attempt` 112-114 | `session.get(code_change_attempts, id)` | Most frequent read: workflow steps, cost status, CLI `changes show` | 10-30 per attempt | strong | PK | no | `GetItem ATTEMPT#<aid>/PTR` (immutable pointer to `request_id`, cacheable for the process lifetime) then `GetItem REQ#<rid>/ATT#<aid>`, both consistent |
| C5 | `lock_attempt` 116-124 | `SELECT ... FOR UPDATE` | Serialise state changes and decisions (service 140, 204) | per transition | strong | - | **row lock** | As C4; the commit conditions on the attempt's `row_version` (already in the model) |
| C6 | `list_attempts` 126-134 | `WHERE request_id = ? ORDER BY number` | Active-attempt check and next attempt number inside the request lock (service 68, 92, 119) | per start | **strong** (inside a lock) | - | yes | `Query PK=REQ#<rid>, begins_with(SK,'ATT#')`, consistent; sort by `number` client-side (tens of items). A GSI would be wrong: it is eventually consistent |
| C7 | `add_attempt` 136-148 | `INSERT code_change_attempts` | New attempt | per attempt | strong | `(request_id, number)` unique (`models/changes.py:49`); **partial unique** one non-terminal attempt per `(request_id, repository_id)` (`models/changes.py:50-56`) | yes | Same commit: `Put REQ#<rid>/ATT#<aid>`, `Put ATTEMPT#<aid>/PTR`, `Put REQ#<rid>/ACTIVE#<repository_id>` (all `attribute_not_exists`). The ACTIVE guard replaces the partial unique index; number uniqueness follows from the request version check |
| C8 | `update_attempt` 150-163 | `UPDATE ... WHERE id = ? AND row_version = ?`, rowcount 1 or `ChangeConflictError` | Every state change, baseline pin, plan and manifest refs | 10-20 per attempt | strong | - | yes | `Update REQ#<rid>/ATT#<aid>` with `row_version = :expected AND NOT #status IN (<terminal>) AND (attribute_not_exists(base_commit_sha) OR base_commit_sha = :sha)` (replaces `attempt_guard` trigger, migration 0001 lines 526-539). A move to a terminal status also deletes the ACTIVE guard in the same transaction |
| C9 | `add_approval` 165-178 | `INSERT code_change_approvals` | Operator decision bound to an artifact hash (service 208) | per gate decision | strong | PK | yes (with C5, C8 on reject) | `Put REQ#<rid>/APR#<aid>#<decided_at>#<approval_id>` (`attribute_not_exists`) plus a `ConditionCheck` (approve) or `Update` (reject) on the attempt version. Append-only: no update/delete path exists (replaces trigger `code_change_approvals_append_only`) |
| C10 | `list_approvals` 180-188 | `WHERE attempt_id = ? ORDER BY decided_at` (index `ix_code_change_approvals_attempt`) | Gate re-checks in every `_mutate` (service 143) and CLI show | per transition | strong | - | inside C5 | `Query PK=REQ#<rid>, begins_with(SK,'APR#<aid>#')`, consistent, ascending |
| C11 | never-delete triggers (`code_change_attempts_never_deleted`, migration 0001 line 549) | DB trigger | Retention | - | - | - | - | No delete code path for ATT/APR items; production IAM denies `DeleteItem` except on guard and job key prefixes (`dynamodb:LeadingKeys`) |

## 3. Multi-repository parents and children; Jira report state

Adapter: `persistence/repositories/multirepo_store.py` (`PostgresMultiRepoStore`). Port:
`application/multirepo/models.py:108-139`. Callers: `application/multirepo/coordinator.py` and
`orchestration/intake.py:157-178`.

| # | Operation (file:line) | Current SQL / op | Purpose | Frequency | Consistency | Uniqueness | Tx | Proposed DynamoDB |
|---|---|---|---|---|---|---|---|---|
| M1 | `create` 72-106 | `INSERT parent ON CONFLICT (request_id) DO NOTHING`, then child `INSERT`s, then M2 | Idempotent parent per request (coordinator 199, 228) | per multi-repo Story | strong | `parents.request_id` unique (`models/multirepo.py:49`); children `(parent_id, repository_id)` and `(parent_id, position)` unique (`models/multirepo.py:72-73`) | **yes** | One transaction: `Put REQ#<rid>/PARENT` (`attribute_not_exists`; one per request by key), `Put PARENT#<pid>/PTR`, per child `Put REQ#<rid>/CHILD#<cid>` and `Put CHILD#<cid>/PTR`. At most 2 + 2*20 = 42 items (limit `multi_repo_max_repositories` le 20, `core/config.py:189`). Child repository and position uniqueness are enforced by the coordinator's checks (coordinator 173-175) and the single atomic create; nothing later rewrites them |
| M2 | `find_by_request` 108-119 | `SELECT parent WHERE request_id` + M4 | Replayed intake never creates a second parent (coordinator 146) | per intake | strong | - | no | `Query PK=REQ#<rid>, SK between 'CHILD#' and 'PARENT'` (one consistent query returns parent and children) |
| M3 | `load` 121-126 | `session.get(parent)` + M4 | Every advance, finish, report, retry (coordinator 238, 277, 305, 482, 536, 543; intake 169) | several per child event | strong | - | no | `GetItem PARENT#<pid>/PTR` (cacheable) then the M2 query |
| M4 | `_children` 128-137 | `WHERE parent_id ORDER BY position` | Children in dependency order | with M2/M3 | strong | - | no | Part of the M2 query; sort by `position` client-side |
| M5 | `child` 139-142 | `session.get(child)` | Child job handler, retry (intake 158; coordinator 402, 523, 554) | per child job | strong | PK | no | `GetItem CHILD#<cid>/PTR` (cacheable) then `GetItem REQ#<rid>/CHILD#<cid>` |
| M6 | `child_by_attempt` 144-153 | `WHERE attempt_id = ?` (index `ix_code_change_children_attempt_id`) | Is this attempt a child, and its context (coordinator 479, 518) | per attempt start (steps) | **strong**: called right after M7 sets `attempt_id` (intake 173-179) | - | no | Base-table link `CHILDATT#<aid>/PTR` -> `(request_id, child_id)` written in the M7 transaction that sets `attempt_id`; on read, verify `child.attempt_id == aid` (a retried child clears it). A GSI here could return "not a child" for a just-linked attempt and run it as a single-repository attempt |
| M7 | `update_child` 155-174 | `UPDATE ... WHERE id AND status IN (expected) RETURNING id` | Compare-and-set child state (coordinator 248-276, 420-422, 528-542; intake 173-178) | per child transition | strong | - | single row | `UpdateItem REQ#<rid>/CHILD#<cid>` with `#status IN (:e1, ...)`; `ConditionalCheckFailedException` -> `False`. When `attempt_id` changes, the same transaction writes or replaces `CHILDATT#<aid>` |
| M8 | `update_parent` 176-203 | CAS on status; `finished` sets `completed_at`; `reopen` resets report fields and `report_round + 1` | Finish and retry (coordinator 290, 544-551) | per parent transition | strong | - | single row | `UpdateItem REQ#<rid>/PARENT` with `#status IN (...)`; `reopen` uses `ADD report_round :one` and `REMOVE jira_report, jira_comment_id, ...` |
| M9 | `claim_report` 205-235 | `SELECT jira_report FOR UPDATE`, then `UPDATE ... WHERE claimable RETURNING report_round, attempts`; claimable = NULL, PENDING, RETRYABLE_FAILURE, or IN_PROGRESS older than `stale_seconds` (300, coordinator 47) | At-most-once Jira comment per round (coordinator 308) | per report | strong | - | yes | One `UpdateItem` with condition `attribute_not_exists(jira_report) OR jira_report IN (:p, :r) OR (jira_report = :ip AND jira_report_at < :stale)`, `ADD jira_report_attempts :one`, `ReturnValues=ALL_OLD` (previous state, round; new attempts = old + 1). `:stale` comes from the injected clock |
| M10 | `finish_report` 237-260 | `UPDATE ... WHERE jira_report = 'IN_PROGRESS'` | Record the report outcome (coordinator 338-340) | per report | strong | - | single row | `UpdateItem` with `jira_report = :ip` |
| M11 | `set_request_status` 262-269 | `UPDATE code_change_requests SET status` | Multi-repo request status projection (coordinator 200, 229, 293, 552) | per parent transition | strong | - | no | `UpdateItem REQ#<rid>/REQUEST SET status, updated_at ADD row_version 1` |

## 4. Jobs: claim, lease, heartbeat, retry, reclaim

Adapter: `persistence/repositories/job_queue.py` (`PostgresJobQueue`). Port: `application/jobs/queue.py:45-68`.
Consumer: `application/jobs/worker.py`. Producers: W6, `coordinator.py:271-276` (child job,
dedupe `code_change.child:<child_id>:<runs>`), `coordinator.py:346-351` (report job, dedupe
`code_change.parent_report:<parent_id>:<round>`), `cli/operations.py:149-153` (resume job, dedupe
`code_change.resume:<attempt_id>:<approval_id>`). Settings: lease 300 s, heartbeat 60 s,
concurrency 2, attempts 3 (`core/config.py:185-188`); backoff 30 s doubling to 900 s
(`worker.py:26-41`).

| # | Operation (file:line) | Current SQL / op | Purpose | Frequency | Consistency | Uniqueness | Tx | Proposed DynamoDB + SQS |
|---|---|---|---|---|---|---|---|---|
| J1 | `enqueue` 55-73 | `INSERT jobs ... ON CONFLICT (dedupe_key) DO NOTHING RETURNING id` -> bool | Durable, deduplicated job creation | per job | strong | `dedupe_key` unique, **permanent** | single row | Transaction: `Put JOBKEY#<dedupe_key>/GUARD` (`attribute_not_exists`, permanent, no TTL) and `Put JOB#<jid>/JOB` (QUEUED, `GSI3` dispatch keys set). Then a best-effort `SendMessage {job_id}` and `REMOVE` of the dispatch keys; the relay (J8) covers a crash in between |
| J2 | `claim` 75-81 with `_CLAIM` 20-28 | `UPDATE ... WHERE id = (SELECT ... WHERE attempts < max AND ((QUEUED AND available_at <= now) OR (RUNNING AND lease_expires_at < now)) ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING ...`; sets RUNNING, owner, lease, heartbeat, `attempts + 1` | Claim one runnable job, including a crashed worker's job | every poll (2 s idle, `worker.py:57`) | strong | one owner | yes | `ReceiveMessage` (long poll 20 s, `VisibilityTimeout = lease`), then conditional `UpdateItem JOB#<jid>` with the same predicate and `ReturnValues=ALL_NEW`. Condition failure: terminal job -> `DeleteMessage`; not yet due -> `ChangeMessageVisibility` to `available_at - now`; live lease elsewhere -> visibility = remaining lease |
| J3 | `_REAP` 15-19 (run inside every claim) | `UPDATE jobs SET DEAD, last_error='lease_expired_attempts_exhausted' WHERE RUNNING AND lease expired AND attempts >= max` | Exhausted crashed jobs become DEAD | every poll | strong | - | yes | Done lazily when the job's message is received again (its visibility expires with the lease) or by the relay sweep (J8): conditional `UpdateItem` to DEAD, then `DeleteMessage` |
| J4 | `heartbeat` 83-88 with `_BEAT` 29-33 | `UPDATE ... SET lease_expires_at = now + lease WHERE id AND RUNNING AND lease_owner AND lease_expires_at >= now` | Keep the lease; `False` cancels the handler (`worker.py:142-147`) | per running job every 60 s | strong | owner | single row | Conditional `UpdateItem` (same predicate, app clock) and `ChangeMessageVisibility(receipt, lease)`. DynamoDB decides; a failed visibility change is logged, never fatal |
| J5 | `complete` 90-93 with `_DONE` 34-38 | `UPDATE SET SUCCEEDED WHERE id AND RUNNING AND lease_owner` | Success | per job | strong | owner | single row | Conditional `UpdateItem` (`SUCCEEDED`, `finished_at`, `REMOVE` GSI3 keys, `expires_at` = now + retention), then `DeleteMessage` |
| J6 | `fail` 95-110 with `_FAIL` 39-48 | Re-queue with `available_at = now + retry` while attempts remain, else DEAD; returns status (`RUNNING` when the lease was lost) | Retry with backoff or dead-letter | per failure | strong | owner | single row | Conditional `UpdateItem` (`QUEUED` + `available_at`, or `DEAD`) with `ReturnValues`; QUEUED -> `ChangeMessageVisibility(receipt, retry)` (retry le 900 s, under the 43,200 s maximum); DEAD -> `DeleteMessage` |
| J7 | index `ix_jobs_claimable` (`models/jobs.py:30-34`) | Partial index on `available_at` for QUEUED/RUNNING | Claim ordering | - | - | - | - | Replaced by SQS delivery; ordering becomes best-effort (standard queue) |
| J8 | (new) relay / sweep | - | Dispatch transactional-outbox jobs (W6, J1) whose message was never sent, re-send for non-terminal jobs whose message was lost or dead-lettered | every 2-5 s (relay), every 60 s (sweep) | eventual (GSI3), then a strong re-check on the item | - | - | Relay: `Query GSI3 PK=DISPATCH, SK <= <now>`; for each, consistent `GetItem` and re-check, then `SendMessage` and move the entry to `PK=SWEEP`. Sweep: `Query GSI3 PK=SWEEP`, consistent re-read, re-send for overdue jobs (due or lease passed by more than the grace period, not re-sent within it). Terminal transitions remove the GSI3 keys. Duplicate messages are harmless (J2 is conditional) |

## 5. Repository registry: repositories, aliases, profile versions, mappings, audit

Adapter: `persistence/repositories/registry_store.py` (`SqlAlchemyRegistryStore`, `SqlAlchemyAuditLog`,
`SqlAlchemyRegistryUnitOfWork` 367-396: one transaction, rollback unless committed). Port:
`application/repositories/ports.py:26-97`. Callers: `application/repositories/service.py` (operator CLI
`cli/main.py:494-511`), `application/repositories/resolver.py:102-165` (per intake and per declared
multi-repo repository), `application/repositories/profile_reader.py:26-27` and
`application/workspaces/registry_baseline.py:46-50` (per attempt). Error codes are part of the contract:
`repository_already_registered`, `repository_key_taken`, `alias_taken`, `profile_version_conflict`,
`publish_conflict`, `repository_state_conflict`, `mapping_conflict` (`registry_store.py:81-86`).

| # | Operation (file:line) | Current SQL / op | Purpose | Frequency | Consistency | Uniqueness | Tx | Proposed DynamoDB |
|---|---|---|---|---|---|---|---|---|
| R1 | `add_repository` 88-113 | `INSERT repositories` + `INSERT repository_aliases (kind KEY)`; flush errors -> codes | Register (service 113-127, plus audit R16) | operator | strong | `repository_key` unique, `canonical_locator` unique, `(git_provider, provider_repository_id)` unique (`models/registry.py:36-38,55,61`); global alias namespace PK (`models/registry.py:107`) | **yes** | One transaction: `Put REPO#<id>/REPO`, `Put ALIAS#<key>/ALIAS` (kind KEY), `Put REPO#<id>/ALIAS#<key>` (reverse copy), `Put LOCATOR#<locator>/GUARD`, `Put PROVIDERREPO#<provider>#<provider_id>/GUARD`, `Put AUDIT#...`; each `attribute_not_exists`. Cancellation index -> same error code as today (ALIAS -> `repository_key_taken`, LOCATOR/PROVIDERREPO -> `repository_already_registered`) |
| R2 | `get_repository` 115-121 | `WHERE repository_key = ?` | Operator commands by key (service 75) | operator | strong | - | in UoW | Consistent `GetItem ALIAS#<key>/ALIAS`, require `kind = KEY`, then `GetItem REPO#<id>/REPO` |
| R3 | `lock_repository` 123-131 | `SELECT ... FOR UPDATE` by key | Serialise registry mutations (service 75 with `lock=True`: enable, disable, add_alias, create_version, publish, activate, map, unmap) | operator | strong | - | **row lock** | As R2; the commit carries `row_version = :seen` on REPO (Update) or a `ConditionCheck` when the operation does not change REPO |
| R4 | `get_repository_by_id` 133-135 | PK get | Resolver (136), baseline (47) | per intake, per attempt | strong | PK | no | `GetItem REPO#<id>/REPO` |
| R5 | `list_repositories` 137-141 | `ORDER BY repository_key` | CLI list (service 129-131) | operator | eventual OK | - | no | `Query GSI2 PK=LIST#REPOSITORY` (SK = key, ascending), paginate |
| R6 | `_bump`/`set_status` 143-155 | `UPDATE SET row_version + 1, status`; check `ACTIVE requires active_profile_version_id` (`models/registry.py:42-45`) | Enable/disable (service 155, 176) | operator | strong | - | yes | `Update REPO` with `row_version = :seen` and, for ACTIVE, `attribute_exists(active_profile_version_id)`; returns ALL_NEW |
| R7 | `add_alias` 157-163 | `INSERT repository_aliases (kind ALIAS)`; conflict -> `alias_taken` | Alias (service 196) | operator | strong | alias PK (global namespace with keys) | yes | `Put ALIAS#<alias>/ALIAS` + `Put REPO#<id>/ALIAS#<alias>` + REPO `ConditionCheck` + audit |
| R8 | `list_aliases` 165-171 | `WHERE repository_id ORDER BY alias` | Show repository (service 141) | operator | strong (base table) | - | no | `Query PK=REPO#<id>, begins_with(SK,'ALIAS#')` |
| R9 | `find_by_alias` 173-183 | `JOIN repository_aliases ON ... WHERE alias = ?` (KEY and ALIAS rows) | Resolve a Story reference (resolver 118) | per intake / declaration | strong | - | no | `GetItem ALIAS#<alias>/ALIAS` then R4 |
| R10 | `find_by_locator` 185-191 | `WHERE canonical_locator = ?` | Resolve `owner/name` or URL (resolver 125) | per intake / declaration | strong | - | no | `GetItem LOCATOR#<locator>/GUARD` then R4 |
| R11 | `add_profile_version` 193-224 | `SELECT coalesce(max(version),0)` under the repo lock, `INSERT version + 1` | New draft version (service 228) | operator | strong | `(repository_id, version)` unique (`models/registry.py:77`) | yes | Transaction: `Update REPO` (`row_version = :seen`, `ADD last_profile_version 1`; the new number = seen + 1), `Put REPO#<id>/PV#<version:06d>` and `Put PROFILEVERSION#<vid>/PTR` (`attribute_not_exists`), audit |
| R12 | `get_profile_version` 226-237 | `WHERE repository_id AND version` | Publish/activate (service 253) | operator | strong | - | in UoW | `GetItem REPO#<id>/PV#<version:06d>` |
| R13 | `get_profile_version_by_id` 239-241 | PK get | Resolver (154), profile reader (27), baseline (50) | per intake, per attempt | strong | PK | no | `GetItem PROFILEVERSION#<vid>/PTR` (immutable, cacheable) then R12 |
| R14 | `list_profile_versions` 243-251 | `ORDER BY version` | Show repository (service 140) | operator | strong | - | no | `Query PK=REPO#<id>, begins_with(SK,'PV#')` |
| R15 | `mark_published` 253-262, `set_active_version` 264-267 | `UPDATE published_at = now()`; `_bump(active_profile_version_id)`; trigger `registry_profile_version_guard` (migration 0001 503-523) allows only draft -> published; FK `(id, active_profile_version_id)` -> same repository (`models/registry.py:46-51`) | Publish and activate (service 275, 287, 310) | operator | strong | - | yes | `Update PV` with `attribute_not_exists(published_at)` (published versions immutable; content attributes are never in any UpdateExpression); `Update REPO` with `row_version`; activation adds a `ConditionCheck` on the PV item (`attribute_exists(published_at) AND repository_id = :rid`) |
| R16 | `set_mapping` 269-301 | `SELECT ... FOR UPDATE` on `(project, repo)`, then INSERT or UPDATE status | Map/unmap a Jira project (service 337, 365) | operator | strong | `(jira_project_key, repository_id)` unique (`models/registry.py:121-123`) | yes | `Put/Update PROJECT#<key>/MAP#<repository_id>` (the key is the unique pair; `row_version` CAS on update), REPO `ConditionCheck`, audit |
| R17 | `list_mappings` 303-320 | Optional filters project / repository / status, `ORDER BY project, created_at` | Resolver without a reference (129-131: project + ACTIVE), authorisation (147-149: project + repository + ACTIVE), map/unmap check (service 332, 360), show (142), CLI list (386) | per intake; operator | **strong** for resolver/authorisation; eventual for display | - | in UoW | project+repo: `GetItem PROJECT#<key>/MAP#<rid>`; project: `Query PK=PROJECT#<key>`; repository only: `Query GSI1 PK=REPOMAPS#<rid>` (display only); none: `Query GSI2 PK=LIST#MAPPING`; status by `FilterExpression` |
| R18 | `SqlAlchemyAuditLog.record` 327-339 | `INSERT registry_audit_events`; trigger `registry_audit_events_append_only` (migration 0001 541) | Audit every registry mutation, same transaction (service 53-71) | operator | strong | PK | **yes, with the mutation** | `Put AUDIT#<entity_type>#<entity_id>/EV#<occurred_at>#<event_id>` in the mutation's transaction (`attribute_not_exists`); append-only by absence of update paths and by IAM in production |
| R19 | `list_events` 341-364 | Optional `entity_type`/`entity_id`, `ORDER BY occurred_at DESC, id LIMIT n` (le 500) | CLI audit (service 390-395 passes both or neither) | operator | eventual OK | - | no | Both: `Query PK=AUDIT#<type>#<id>, ScanIndexForward=false, Limit=n`; neither: `Query GSI2 PK=LIST#AUDIT` descending with `Limit` |
| R20 | `repositories_never_deleted`, profile-version delete guard | Triggers | Retention | - | - | - | - | No delete paths; production IAM |

## 6. LLM usage ledger and cost aggregation

Adapter: `persistence/repositories/usage_store.py` (`PostgresUsageStore`). Ports:
`application/costs/usage.py:61-64` (`UsageStore.add`), `application/costs/report.py:40-47` (`CostQuery.rows`).

| # | Operation (file:line) | Current SQL / op | Purpose | Frequency | Consistency | Uniqueness | Tx | Proposed DynamoDB |
|---|---|---|---|---|---|---|---|---|
| U1 | `add` 34-65 | `INSERT llm_usage_records ... ON CONFLICT DO NOTHING RETURNING id` -> bool | Append one LLM call (callers `costs/usage.py:216-221` per model response, `costs/usage.py:328` per search) | **per LLM call** (about 18 per attempt) | strong | `id` (call id) PK; `provider_message_id` unique when not null (`models/usage.py:35,43`); append-only trigger (migration 0002 71) | single row | Transaction (3-4 items): `Put LLMCALL#<call_id>/GUARD`, `Put LLMMSG#<provider_message_id>/GUARD` (when present), `Put USAGE#ATT#<aid>/<created_at>#<call_id>` (when `attempt_id` is set), `Put USAGE#SES#<session_id>/<created_at>#<call_id>`; all `attribute_not_exists`. Any guard conflict -> `False`. `cost_usd` stored as a decimal string (exact `Numeric(16,10)` text) |
| U2 | `rows(attempt_ids=...)` 67-86, `_ROWS` 16-27 | `LEFT JOIN code_change_attempts, code_change_requests, repositories`; `WHERE u.attempt_id IN (...)` `ORDER BY created_at, id` | Task report when an attempt stops (`costs/service.py:55`), CLI `costs --attempt` (`cli/operations.py:93-94`) | per attempt end; operator | **strong** (report is printed right after the last call is recorded) | - | no | Per attempt `Query PK=USAGE#ATT#<aid>` (consistent, ascending); join data: C4 for `number`/`status` (current status, so never denormalised), C1 for `jira_issue_key`, R4 for `repository_key` (BatchGetItem, cached per call) |
| U3 | `rows(issue_keys=...)` | `WHERE r.jira_issue_key IN (...)` | Whole-Story totals (`costs/service.py:57,77`), CLI `costs --issue` (`cli/operations.py:87-92`), multi-repo summary `cost_of_issue` (`composition.py:551-552`) | per attempt end; per parent report; operator | eventual for issue -> requests is acceptable (requests exist long before); strong per attempt | - | no | `Query GSI1 PK=ISSUEKEY#<key>` -> request ids; per request `Query PK=REQ#<rid>, begins_with(SK,'ATT#')` (consistent) -> attempt ids; then U2 per attempt |
| U4 | `rows(session_id=...)` | `WHERE u.session_id = ?` | Worker session summary at shutdown (`costs/service.py:75`) and CLI `costs --session` | per worker run | strong | - | no | `Query PK=USAGE#SES#<sid>` (consistent, ascending) plus the U2 joins |
| U5 | `rows()` with no filter (`all_attempts` and `all_issues` true) | Whole ledger | **No caller** in `src` or `studio` | - | - | - | - | Not implemented (adapter raises `ValueError` if called without a filter) |
| U6 | `studio/history.py:139-144` `_USAGE` | `SELECT ... FROM llm_usage_records WHERE attempt_id = ANY(:ids)` | Studio attempt list with costs | dev only | eventual OK | - | no | U2 per listed attempt through the `CostQuery` port (no SQL in studio) |

## 7. LangGraph checkpoints (workflow and Deep Agent)

| # | Operation (file:line) | Current op | Purpose | Frequency | Consistency | Size | Proposed |
|---|---|---|---|---|---|---|---|
| L1 | `orchestration/langgraph/checkpointer.py:18-37` | `AsyncPostgresSaver` in schema `langgraph` (ADR C8), `setup()` at worker start (`composition.py:468`) | Workflow graph (`thread_id = attempt:<aid>`, `orchestration/langgraph/runner.py:9-10`) and Deep Agent phase threads (`attempt:<aid>:planning:<run>`, `attempt:<aid>:implementation:<repairs>:<run>`, `orchestration/steps.py:447,603-604`) | every super-step: hundreds per attempt (948 checkpoints, 2,350 writes for 19 attempts) | **strong**: a resume job on another worker reads the checkpoint the previous run wrote | blobs up to 1.2 MB measured | `DynamoDBSaver` from `langgraph-checkpoint-aws==1.3.0`, table `acca-checkpoints`, `consistent_read=True`, S3 offload for payloads over 358,400 bytes (350 KiB; data model section 9.2). Verified on Floci (data model section 6) |
| L2 | `runner.py:18-23` `aget_state` then `ainvoke`; `runner.py:26-30` `Command(resume=True)`; `runner.py:32-39` interrupts | Saver reads/writes | Start once, resume after a gate (`orchestration/langgraph/nodes.py:43` `interrupt`) | per job | strong | - | Same saver API; verified interrupt/resume across two saver instances |
| L3 | `studio/live.py:86-93` | `SELECT thread_id, max(checkpoint_id) FROM checkpoints WHERE thread_id LIKE 'attempt:<aid>:%' AND checkpoint_ns = '' GROUP BY thread_id` | Find an attempt's agent threads | dev only | eventual OK | - | DynamoDBSaver keys threads as `CHECKPOINT_<thread_id>` (no prefix query). Write a thread index item `ATTEMPT#<aid>/THREAD#<thread_id>` when a phase run starts; studio queries `begins_with(SK,'THREAD#')` |
| L4 | `studio/live.py:33-46,104` | Read-only `AsyncPostgresSaver`, `aget_state` per thread | Show live agent state | dev only | eventual OK | - | Same DynamoDBSaver (read-only use) |

## 8. Other direct SQL (studio)

| # | Operation (file:line) | Current SQL | Purpose | Proposed |
|---|---|---|---|---|
| S1 | `studio/history.py:128-138` `_ATTEMPTS` | All attempts joined to request and repository, `ORDER BY a.created_at DESC LIMIT :limit` (le 200) | Studio attempt list | `Query GSI2 PK=LIST#ATTEMPT`, descending, `Limit`; joins by C1 and R4. Single small partition (dev volume) |
| S2 | `studio/history.py:183-186` | `SELECT id, request_id FROM code_change_attempts WHERE id = ANY(:ids)` | Find artifact folders | The GSI2 item carries `request_id` |
| S3 | `studio/live.py:57-63` `_attempt_row` | Attempt joined to request and repository by id | Live view header | C4 + C1 + R4 |
| S4 | `studio/history.py:148-166` `_pr_url` | Reads `ARTIFACT_ROOT/<request>/<attempt>/*.meta.json` from disk | PR link | Through the `ArtifactStore` port (S3 or filesystem) |

## 9. Patterns that do not exist (do not build them)

- No query reads Jira deliveries back (W7), lists all requests, lists jobs, or reads the whole usage
  ledger (U5). No code deletes attempts, approvals, parents, children, repositories, profile versions,
  audit events or usage records.
- No full-text or range query on payloads; LangGraph checkpoint metadata filters (`alist(filter=...)`)
  are not used by the application (only `aget_state` and `ainvoke`).
