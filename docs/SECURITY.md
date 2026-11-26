# Security model

| Boundary | Control |
|---|---|
| Jira -> API | HMAC-SHA256 over the raw body (`X-Hub-Signature`), body size cap, content type, project/issue-type/event scope, delivery-id dedupe in the database |
| Story text | Untrusted: never grants access; the repository reference is parsed to a locator and must match the registry; Story text reaches the agent only inside marked data blocks |
| Repository access | GitHub App installation selection (checked by a fresh token exchange per operation) plus registry (ACTIVE, project mapping, published profile; onboarding is one `register` command and no repository is named in code); no static allowlist; no automatic registration |
| GitHub credentials | Private key file (0600, owner, RSA >= 2048) read only by the worker; read-only token for baseline, write token only in the publisher, metadata token for registration; never in logs, artifacts, URLs, sandboxes or prompts |
| Baseline | Pinned commit; full tree recomputed; tarball verified file by file (size, mode, Git blob SHA, commit id); symlinks, submodules, unsafe or case-colliding paths and oversize snapshots refused |
| Deep Agent sandbox | `--network none`, non-root `--user`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, Docker default seccomp, CPU/memory/PID/nofile limits, size-limited `/tmp` tmpfs, per-command `timeout -s KILL`, output cap, workspace disk check, no environment from the application, no Docker socket, no host paths; planning mounts the workspace read-only |
| Agent tools | The SDK's sandbox-backed tools plus one harness-side `fetch_reference` (ADR-011): runs in the worker, https only, `REFERENCE_ALLOWED_DOMAINS`, public addresses only, re-checked redirects, size/count limits, untrusted-data markers. No Git/GitHub/Jira tools |
| Dependencies | ADR-010: validated requests; wheels-only resolver container (single index, no app credentials); hash lock; offline `--require-hashes` install; immutable cached bundle re-verified on reuse; mounted read-only at `/opt/deps` into network-less sandboxes |
| Validation | Independent of any model; platform-extracted changeset; protected paths, plan scope, limits, secrets; fresh workspace; only profile commands re-checked against the command policy; network commands never run |
| Approvals | Bound to the exact plan or manifest SHA-256; a changed artifact invalidates them; append-only. Automatic mode records `policy:automatic` decisions with stored reasons (PLAN: dependency, path, limit and test-command policy; FINAL: validation passed and tests ran). Operator actor unverified until SSO |
| Delivery | Off by default (two switches); re-checks registry authorisation and installation membership; create-only refs; draft PR; never merge, force or touch the default branch |
| Jira writes | One comment per outcome, only if enabled; no transitions or field edits |
| API container | Non-root, read-only root, capabilities dropped; receives only webhook settings (no GitHub, LLM or Jira REST credentials) |
| Worker | Trusted control plane: has Docker daemon access and credentials; never exposes them to sandboxes |

Known residual risks:

1. Docker Desktop shares the host kernel through a VM. A container escape would reach that VM. Use a dedicated worker host in production.
2. The sandbox disk limit is checked after each command, not enforced by the filesystem. A single command can briefly exceed it. The `/tmp` tmpfs is hard-limited.
3. Operator identity is unauthenticated until SSO.
4. Revocation timing for already-issued GitHub installation tokens is GitHub behaviour and has not been verified.
5. The model sees repository content, which may contain prompt injection. Controls are the quoting, the sandbox and independent validation; automatic policy gates and independent validation check every plan and result, and a human reviews every draft PR before merge.

## S2045 additions

- **Multi-repository authorisation:** every declared repository is resolved through the registry before any work. One failure blocks the whole Story: no clone, token or model call. A child's GitHub token is scoped to its own repository and never reaches an agent. Workspaces are never shared.
- **Contracts:** they are read-only context for dependents. They are stored immutably, hash-checked on every use and refused if they contain a secret.
- **Web research:**
  - it runs in the worker only;
  - search is restricted to the allow-listed domains, and the results are filtered again by host;
  - queries that look like they contain secrets are refused;
  - fetches are https-only, with DNS, private-IP, redirect, size and type checks;
  - all fetched content is marked untrusted;
  - the coding and validation containers stay offline.
- **Cost guards:** model calls and USD are capped per phase run, and structured-output retries are bounded, for any model.
