# GitHub App setup

Create a GitHub App, install it on the repositories the platform may change, and give its id, installation id and private key path to the worker.

| Item | Value |
|---|---|
| App name | AI Code Change Agent |
| App ID | `GITHUB_APP_ID` |
| Installation ID | `GITHUB_APP_INSTALLATION_ID` (also the default access reference for `register`) |
| Account | the account that installed the App |
| Repository selection | "Selected repositories". Granting a repository to the App is the first onboarding step; nothing in the code lists repositories |
| Permissions required | Metadata: read; Contents: read & write; Pull requests: read & write |
| Webhooks | Not used (no App webhook secret needed) |
| Private key | Unchanged file. `GITHUB_APP_PRIVATE_KEY_PATH` is an absolute path; mode 0600, owned by the worker user, RSA >= 2048. The key is never copied into the project, a container or a sandbox. |

## How the platform uses the App

1. **App JWT.** RS256 signed with the private key. Valid for 9 minutes; `iat` is backdated 60 seconds.
2. **Installation tokens.** Each one is scoped to the single registered repository id, with the least permissions for the operation:

   | Use | Permissions |
   |---|---|
   | Baseline read | `metadata:read`, `contents:read` |
   | Registration helper | `metadata:read` |
   | Publishing | `contents:write`, `pull_requests:write` |

   Every operation exchanges a fresh token, so a repository removed from the installation is refused at once. GitHub's granted permission and repository set is verified again on every exchange.
3. **Baseline.** The registered default branch is pinned to one commit. The tree is read and verified, and the tarball is fetched from `codeload.github.com` without the bearer token, then verified file by file.
4. **Publishing.** Only after the final approval, and only with `CODE_CHANGE_DELIVERY_ENABLED=true` and `GITHUB_PR_CREATE_ENABLED=true`:
   - create blobs, the tree and the commit, plus the branch ref (create-only);
   - open a **draft** PR;
   - re-read the PR to verify it.

   Never merge, force-push or write to the default branch.

## Local verification procedure

```bash
uv run python -c "from ai_code_change_agent.core.config import Settings; s=Settings(); print(s.github_app_configured)"
uv run ai-code-change repositories register --from-provider <owner>/<repo>
```

The second command is a read-only identity lookup plus a local DISABLED registry row with the standard profile; add `--project <KEY> --enable` to make it usable.

## Verification status

Live: repository registration, pinned baselines and draft PRs on four repositories (KAN-7, KAN-8, KAN-9).
