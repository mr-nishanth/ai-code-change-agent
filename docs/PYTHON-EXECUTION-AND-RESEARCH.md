# Python execution and documentation research

Repository execution is **Python only**. The coding agent and the independent validation engine run every repository command in one Python execution image. Repositories in other languages (Node.js, React/Next.js, Android, Kotlin/Java, Flutter, iOS/Swift) are out of scope: a profile that names any toolchain other than `python` is BLOCKED (`no_execution_image_for_toolchain`) before any work.

## Execution image

| Item | Value |
|---|---|
| Image | `ai-code-change-agent/execution-python:0.2.0` (`WORKSPACE_IMAGE`) |
| Local image id | `sha256:da4b7d657e0edad2f9f061e25e13083d8f610b1b1e552f9475a4a7f274f87aa4` (a local build id, not a registry digest) |
| Built from | `docker/execution/Dockerfile` |
| Base | `python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b` |
| Tools | Python 3.12.16, pytest 9.2.0, ruff 0.17.6, mypy 2.4.1 |
| Packages | pinned with `--require-hashes` from `docker/execution/requirements.txt` |

Build it once: `docker build -t ai-code-change-agent/execution-python:0.2.0 docker/execution`.

## Allowed commands

The command policy has one toolchain family, `python`. Profile commands must be one of:
- `pytest` or `python -m pytest`;
- `ruff check`, `ruff format --check`;
- `mypy`.

Anything else (unknown toolchain, executable, flag or shape) fails closed. `ai-code-change repositories init-profile <key>` builds the standard Python profile with the test command `python -m pytest -q`; the CLI `--toolchain` flag accepts only `python`.

## Sandbox

The coding sandbox and the validation container use the same hardening:
- `--network none`, read-only root filesystem, all capabilities dropped, `no-new-privileges`, default seccomp profile, non-root user, no credentials;
- limits: `WORKSPACE_CPU_LIMIT=1.0`, `WORKSPACE_MEMORY_LIMIT_MB=2048`, `WORKSPACE_PIDS_LIMIT=256`, `WORKSPACE_DISK_LIMIT_MB=1024`, `WORKSPACE_TIMEOUT_SECONDS=7200`, `DEEP_AGENT_EXECUTE_TIMEOUT_SECONDS=300` per agent command.

## Dependency preparation (ADR-010)

The sandbox never gets network. The agent may only *request* Python packages in its plan: a PEP 508 name with optional version specifiers. URLs, file paths, VCS references, extras and environment markers are refused, as are duplicates after name normalisation. Trusted worker code then resolves and downloads **wheels only** from `DEPENDENCY_INDEX_URL` (default `https://pypi.org/simple`), hash-locks them and mounts the bundle read-only into the sandbox and validation. Limits: `DEPENDENCY_MAX_PACKAGES=60`, `DEPENDENCY_MAX_BYTES=314572800`, `DEPENDENCY_TIMEOUT_SECONDS=600`. Anything else fails closed. Details: [DYNAMIC-DEPENDENCY-PROVISIONING.md](DYNAMIC-DEPENDENCY-PROVISIONING.md), [ADR-010](decisions/ADR-010-dynamic-dependency-provisioning.md).

## Documentation research (harness side)


The coding sandbox stays offline. Two research tools run in the worker:
- **`search_docs(query)`:**
  - one Anthropic web search (`web_search_20250305`, verified with claude-haiku-5-5) restricted to `REFERENCE_ALLOWED_DOMAINS`;
  - results are filtered again by host, with official documentation ranked before package registries and raw source hosts;
  - it returns titles, URLs and a retrieval timestamp;
  - a query that looks like it contains a secret is refused;
  - limited to `REFERENCE_MAX_SEARCHES` per phase;
  - each search is recorded in the cost ledger as stage `research` (tokens plus $0.01 per search).
- **`fetch_reference(url)`:**
  - https only on the default port, no credentials in the URL, allow-listed domains (`REFERENCE_ALLOWED_DOMAINS`; empty means no fetch tool), at most `REFERENCE_MAX_FETCHES` per phase;
  - GET only, no cookies, authorization headers or environment proxies;
  - DNS is resolved and private, loopback and link-local addresses are refused;
  - redirects are followed manually, at most three, and every hop is re-checked;
  - text-like content types only, with a byte cap, a character cap and a timeout (truncation is reported);
  - HTML is reduced to text and the result is marked as untrusted.

The agent is told to cite the URLs and retrieval dates it relied on in its report's `references`.

Web search runs only when `LLM_PROVIDER=anthropic` and `REFERENCE_SEARCH_ENABLED=true`. On Bedrock only `fetch_reference` is available. The search provider sits behind the `ReferenceSearcher` port, so it can be replaced without changing the agent.
See also [ADR-011](decisions/ADR-011-harness-side-reference-fetching.md).
