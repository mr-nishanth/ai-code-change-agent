# Automated Jira-to-draft-PR pipeline

An Ops user creates a Jira Story that names a repository. The platform plans, prepares dependencies, implements, validates, opens a **draft** pull request and comments on the Story, with no human step. The only human decision is reviewing and merging the draft PR. Nothing is ever merged automatically.

## Flow

```
Jira issue_created ─► API (HMAC, dedupe) ─► intake job (DynamoDB + SQS)
   ─► Jira GET issue ─► resolve "Repository: <name|owner/name>" ─► registry: registered, ACTIVE,
      mapped to the project, published profile (newer profile adopted between attempts)
      └─ not resolvable ─► Jira comment "could not start" (no attempt)
   ─► attempt: pin default-branch commit (fresh GitHub App token = installation access check)
   ─► PLAN  (Deep Agent, read-only sandbox, may fetch allow-listed docs)
   ─► PLAN policy gate (automatic) ─ reject ─► REJECTED ─► Jira comment
   ─► dependency bundle (wheels only, hash-locked, offline install, cached) ─ fail ─► FAILED
   ─► IMPLEMENT (writable sandbox, bundle read-only at /opt/deps, --network none)
   ─► independent VALIDATION (fresh container, profile commands; tests must run and pass)
        └─ failed ─► bounded repair (MAX_REPAIR_ATTEMPTS) ─► still failing ─► FAILED ─► Jira comment
   ─► FINAL policy gate (automatic)
   ─► DELIVER: create-only branch, commit, draft PR (reconciles on retry, never force-pushes)
   ─► REPORT: one Jira comment with repository, branch, draft PR URL, summary, checks
```

## Automatic policy gates

Gate decisions are recorded exactly like operator approvals: bound to the plan or manifest SHA-256, append-only, with actor `policy:automatic`. The reasons are stored as an `automatic_decision` artifact. A changed artifact invalidates the decision.

| Gate | Rejects when |
|---|---|
| PLAN | the dependency request is invalid (URL, path, VCS, extras, markers, duplicates, over limit); a planned path is unsafe or protected (`.git`, `.github`, CI, secrets…); more files than the profile allows; **the profile defines no test command** |
| FINAL | independent validation did not pass; no test command ran or a test command did not pass; the manifest does not match the approved plan |

`WORKFLOW_APPROVAL_MODE=manual` restores operator gates (`ai-code-change changes approve|reject`), for exceptional or recovery use. `changes reject --reason TEXT` (optional, one line, at most 500 characters) records why; the Jira comment shows it as "Reason given by the operator: ..." (withheld when it looks like a secret), otherwise "(no rejection reason is recorded)". The comment shows up to 500 characters of it; a multi-repository Story comment shows up to 300 per child, within that comment's detail budget.

## What is checked independently (validation engine)

Before the plan is stored and approved, the platform completes its scope deterministically: an existing file that the plan's own `findings` or `todo` say must change (no negation), or a path named in a `tests` entry (an existing file, or a new `test_*.py`/`*_test.py` module), is added to `changes` with a "added by the platform" purpose when it is missing; protected paths are never added, and the PLAN policy and validation treat the added entries like any other. The implementation prompt forbids touching any other file (a repair cannot widen the plan). A failed validation's Jira comment lists the failed checks with their detail (for example the files outside the approved plan) and the commands that did not pass, never file content or command output.

The rebuilt tree must equal baseline + changes. Also checked: path safety, protected paths, plan scope, change limits and secrets in the diff. Every profile command (re-checked against the command policy) must pass in a fresh, network-less container with the same dependency bundle. **A profile without a test command can never pass** (`profile_defines_no_test_command`). The agent's own report is never an input.

## Failure, retry and idempotency

| Situation | Behaviour |
|---|---|
| Duplicate webhook or duplicate Story | API dedupe on delivery id and issue id (200 `duplicate`); no second request or job |
| Jira read at intake fails | A permanent error (400 and other 4xx, 401, 403, 501/505, a redirect, an oversized or wrongly shaped response) makes the intake job DEAD after one attempt with the error's code (for example `jira_authentication_failed`); 404 stays `jira_issue_not_found`. A transient error (429, 408, 425, 5xx, timeout, connection, a non-JSON page such as a proxy or maintenance page, a malformed JSON body) is retried by the job queue within the job's attempts, with exponential backoff raised to the server's `Retry-After` when given. The delay is capped at 900 s, so a longer `Retry-After` retries inside the server's window and may exhaust the job's attempts |
| Intake refused or dead | A Story intake refuses (repository not resolvable, for example `repository_unknown_repository`; invalid declarations, including a blocked multi-repository start; project changed; a permanent Jira read error) leaves the intake job DEAD with that code. An intake job the worker records DEAD any other way (a transient Jira error, a storage failure or an unexpected exception on its last attempt) is treated the same. So is a job whose worker died on its last attempt (the next claim records it DEAD, `lease_expired_attempts_exhausted`): every DEAD path runs the job kind's on_dead hook ([DYNAMODB-DATA-MODEL.md 8.5](aws/DYNAMODB-DATA-MODEL.md)). Either way no attempt will start from this intake, which is never re-run, so the request moves from RECEIVED to BLOCKED (idempotent). A refusal is reported once with its reason; an intake that died for any other reason gets one "could not start" comment with the job's code; both use the same durable report (`intake-<rid>`), so they never add up to two. An intake that died while running the workflow it started ends that attempt instead (next row) |
| A job dies with attempts exhausted | A resume, child or intake job that ended DEAD while its attempt was not terminal ends the attempt: FAILED `job_attempts_exhausted` (the active-attempt guard is released, a new intake may start a new attempt), reported once per attempt; an attempt in REPORTING (its pull request exists) completes through the report step instead. A multi-repository child is recorded FAILED and its parent advances, finishes and posts its aggregated comment once. A worker that lost its lease cannot overwrite any of this (its writes are conditional on its lease) |
| Worker shutdown (SIGINT, SIGTERM) | Both stop claiming. A job claimed during the stop is given back at once with its attempt refunded; a running job gets 30 s to finish, then (or at once on a second signal) it is cancelled and its lease released, so another worker or a restart resumes it immediately instead of after the lease. Bound: one SQS long poll (at most 20 s) plus the grace |
| LLM timeouts, 429/5xx | SDK retries (`LLM_MAX_RETRIES`; Anthropic SDK backoff, botocore standard retries on Bedrock); per-phase wall-clock budget `DEEP_AGENT_RUN_TIMEOUT_SECONDS` → `agent_run_timeout` |
| Story needs clarification | The planner may answer a vague or ambiguous Story with `needs_clarification` and up to 10 questions instead of a plan. The attempt stops before any approval, in both approval modes: FAILED `story_needs_clarification`, no implementation, no pull request, and the Jira comment lists the questions. Not retryable until the Story is updated |
| Dependency failure | `dependency_*` code (wheel unavailable, conflict, Python unsupported, limit, corrupted bundle) → attempt FAILED, Jira comment; nothing falls back to network |
| Validation failure | A code failure (a check or command failed, or a command exceeded the profile's per-command timeout) gets one bounded repair with the validator's feedback, then FAILED (`validation_failed`). A timeout counts as a code failure because the change itself is the usual cause (a hang, or a call blocking under `--network none`). Nothing is published |
| Validation infrastructure failure | Docker unavailable, a container that did not start (exit 125/126), or a sandbox or workspace runtime failure: FAILED `validation_infrastructure_unavailable` with no repair run; retryable once the cause is resolved |
| Validation command not found | A profile command's executable is missing from the image (exit 127): FAILED `validation_command_not_found` with no repair run (the agent cannot install tools in the network-less sandbox); not retryable until the profile or image is fixed |
| Known limitation: 126/127 caused by the change | Exit 126 and 127 are classified without looking at the change. A profile command that runs a repository script (`./scripts/test.sh`) which the change deleted (127) or made non-executable (126) is reported as `validation_command_not_found` or `validation_infrastructure_unavailable` and gets no repair run, although the change caused it; a retry of the 126 case fails the same way. Profile commands are usually tools from the image, so this is rare |
| GitHub timeout or unknown outcome | The publisher re-reads remote state before every write and reconciles; create-only refs; no duplicate branch, commit or PR |
| Jira comment | Exactly one per attempt outcome (`attempt-<aid>`) or per intake that started nothing (`intake-<rid>`), durable: see "Jira reporting" below. Multi-repository parents: one per report round, with their own durable lifecycle and Report-ID reconciliation ([MULTI-REPOSITORY.md](MULTI-REPOSITORY.md#jira-reporting-parent-story)). A reporting failure never changes the outcome: the attempt's status and the report's state are separate fields |
| Security or policy failures | Never retried automatically |
| Re-running a Story | A new attempt is created only by a new intake job; it adopts the newest published profile |

## Jira reporting

Every app-posted comment is stored as a report (`JIRAREPORT#<id>`, [DYNAMODB-DATA-MODEL.md 8.6](aws/DYNAMODB-DATA-MODEL.md)) **before** it is posted, then delivered:

- **States:** PENDING -> IN_PROGRESS -> POSTED, FAILED_PERMANENT, or back to PENDING with `next_retry_at` and `last_error`. The attempt (or request) status never depends on it.
- **Transient** (429, 5xx, timeout, connection error): up to 3 POSTs in the call (429 waits its `Retry-After` up to 10 s; otherwise 1 s, 2 s with jitter). If they all fail, the next round is scheduled 30 s later, doubling to 900 s (+-25 % jitter, never before `Retry-After`); the worker's dispatcher picks due reports up on its relay tick, across restarts. After 6 rounds the report is FAILED_PERMANENT (`retries_exhausted:<code>`).
- **Permanent** (400, 401, 403, 404, 405, 410, 413, 422, invalid key): FAILED_PERMANENT at once, never retried.
- **No duplicates:** Jira has no idempotency key, so the comment ends with `Report ID: aica-report-<id>`. Before any POST that might repeat an earlier one (a later try or round, or a claim left by a crashed worker, taken over after 300 s), the issue's newest 100 comments are searched for the marker; a match is recorded as POSTED and nothing is posted. A failed lookup never leads to a blind POST.
- **Observability:** `jira_report_pending`, `jira_report_retry`, `jira_report_retry_scheduled`, `jira_report_posted`, `jira_report_failed_permanent` in the worker log (codes only, never the text); `ai-code-change reports list [--state PENDING|FAILED_PERMANENT]` and `reports show <id>`.
- **Residual limitation:** the marker search reads only the newest 100 comments. If Jira created a comment whose reply was lost and more than 100 newer comments are added before the retry, the report is posted a second time. A permanently failed report is not re-posted automatically.

## LLM cost

Every model call is recorded and priced from provider-reported usage. The worker prints a task cost report when an attempt ends and a session summary when it stops (`ai-code-change costs --issue KEY` prints the reports on demand). See [LLM-COST-TRACKING.md](LLM-COST-TRACKING.md).

## Configuration (production-like)

```env
WORKFLOW_APPROVAL_MODE=automatic
CODE_CHANGE_DELIVERY_ENABLED=true
GITHUB_PR_CREATE_ENABLED=true
JIRA_REPORTING_ENABLED=true
DEPENDENCY_INDEX_URL=https://pypi.org/simple
DEPENDENCY_MAX_PACKAGES=60
DEPENDENCY_MAX_BYTES=314572800
DEPENDENCY_TIMEOUT_SECONDS=600
REFERENCE_MAX_FETCHES=20
DEEP_AGENT_RUN_TIMEOUT_SECONDS=5400
MAX_REPAIR_ATTEMPTS=1
```

Keep delivery and reporting **false** when evaluating a new model or repository. The pipeline then stops before publishing, with the attempt waiting at the FINAL gate.

## Onboarding a repository (operator, no redeploy)

Grant the repository to the GitHub App, then run:

```bash
# one command: register (identity read from GitHub), standard profile, Jira mapping, enable
uv run ai-code-change repositories register --from-provider <owner>/<repo> --project <JIRA KEY> --enable
```

- **Key, name, access:** the key is derived from the repository name (`My_Repo` → `my-repo`), the display name defaults to the repository name and the access reference to `app-installation:<GITHUB_APP_INSTALLATION_ID>`. Override with `--key`, `--display-name`, `--access-ref`.
- **Profile:** the standard profile for `--toolchain` (default `python`) is created and published, with its test command (`python -m pytest -q`). Use `--no-profile` to supply your own (`create-version` + `publish`), or `init-profile <key> --test-command "..."` later to replace it.
- **Test command fallback:** a profile without a test command still gets its toolchain's standard test command at runtime, so no repository needs per-repository configuration.

Stories then name it with a line `Repository: <key>` or `Repository: <owner>/<repo>`.

Nothing in the code is specific to a repository: repositories, profiles and project mappings are registry data, so adding a repository never needs a code change or redeploy. The PLAN gate still rejects a plan when the effective profile has no test command (`profile_defines_no_test_command`), which can only happen for a toolchain without a standard command.

## Several repositories in one Story

Declare each repository with its own `Repository:` line, optionally with `Role:`, `Responsibility:` and `Depends on:`. The worker runs one isolated child per repository in dependency order, hands upstream contracts to dependents and posts one aggregated Jira comment. See [MULTI-REPOSITORY.md](MULTI-REPOSITORY.md).
