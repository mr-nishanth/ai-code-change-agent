# Automated Jira-to-draft-PR pipeline

An Ops user creates a Jira Story that names a repository. The platform plans, prepares dependencies, implements, validates, opens a **draft** pull request and comments on the Story, with no human step. The only human decision is reviewing and merging the draft PR. Nothing is ever merged automatically.

## Flow

```
Jira issue_created ─► API (HMAC, dedupe) ─► intake job (PostgreSQL)
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

`WORKFLOW_APPROVAL_MODE=manual` restores operator gates (`ai-code-change changes approve|reject`), for exceptional or recovery use.

## What is checked independently (validation engine)

The rebuilt tree must equal baseline + changes. Also checked: path safety, protected paths, plan scope, change limits and secrets in the diff. Every profile command (re-checked against the command policy) must pass in a fresh, network-less container with the same dependency bundle. **A profile without a test command can never pass** (`profile_defines_no_test_command`). The agent's own report is never an input.

## Failure, retry and idempotency

| Situation | Behaviour |
|---|---|
| Duplicate webhook or duplicate Story | API dedupe on delivery id and issue id (200 `duplicate`); no second request or job |
| LLM timeouts, 429/5xx | SDK retries (`LLM_MAX_RETRIES`; Anthropic SDK backoff, botocore standard retries on Bedrock); per-phase wall-clock budget `DEEP_AGENT_RUN_TIMEOUT_SECONDS` → `agent_run_timeout` |
| Dependency failure | `dependency_*` code (wheel unavailable, conflict, Python unsupported, limit, corrupted bundle) → attempt FAILED, Jira comment; nothing falls back to network |
| Validation failure | Bounded repair with the validator's feedback, then FAILED; nothing is published |
| GitHub timeout or unknown outcome | The publisher re-reads remote state before every write and reconciles; create-only refs; no duplicate branch, commit or PR |
| Jira comment | At most one per attempt outcome (`jira_report` artifact marker); a reporting failure never changes the outcome |
| Security or policy failures | Never retried automatically |
| Re-running a Story | A new attempt is created only by a new intake job; it adopts the newest published profile |

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
REFERENCE_ALLOWED_DOMAINS=docs.langchain.com,docs.python.org,fastapi.tiangolo.com,docs.pydantic.dev,pypi.org
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
