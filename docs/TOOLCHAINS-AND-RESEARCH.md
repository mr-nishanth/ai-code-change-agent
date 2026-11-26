# Toolchains and documentation research (S2045)

## Toolchain execution images

Each profile names a toolchain, and the sandbox and the validation runner both use that toolchain's image:
- **python:** `WORKSPACE_IMAGE`;
- **other toolchains:** `WORKSPACE_IMAGES=node=<image>,...`;
- **no image configured:** the attempt is BLOCKED (`no_execution_image_for_toolchain`) before any work.

All images run with the same hardening: no network, read-only root filesystem, all capabilities dropped, no-new-privileges, non-root user, resource limits and no credentials.

| Toolchain | Image | Status | Test command |
|---|---|---|---|
| python | `ai-code-change-agent/execution-python:0.2.0` | **Verified** (KAN-7, KAN-8, KAN-9) | `python -m pytest -q` |
| node | `ai-code-change-agent/execution-node:0.1.0` (node 22.23.5, npm 10.9.9; base `node:22-bookworm-slim@sha256:be979e55…`; image `sha256:73f23171…`) | **Verified** in a Docker integration test: `npm test` runs offline in the sandbox and in validation | `npm test` (the repository's own script) |
| android | - | **Not supported yet.** Needs an Android SDK + JDK image and Gradle dependency bundles, which are not built | - |
| flutter | - | **Not supported yet.** Needs a Flutter SDK image and pub dependency bundles | - |
| ios | - | **Unsupported on Linux.** Xcode builds need a macOS runner, which is not implemented; no iOS build is ever claimed | - |

- **Node dependencies:** a Node repository whose tests need packages from `node_modules` fails validation offline, because the npm dependency-bundle preparer (ADR-010 for npm) is not implemented. The sandbox is never given network instead.
- **Onboarding:** `register --from-provider <owner>/<repo> --toolchain node --project <KEY> --enable` publishes the standard node profile (`npm test`).

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
  - https only, allow-listed domains;
  - DNS is resolved and private, loopback and link-local addresses are refused;
  - every redirect hop is re-checked;
  - size, timeout and content-type limits apply;
  - HTML is reduced to text and the result is marked as untrusted.

The agent is told to cite the URLs and retrieval dates it relied on in its report's `references`.

Web search runs only when `LLM_PROVIDER=anthropic` and `REFERENCE_SEARCH_ENABLED=true`. On Bedrock only `fetch_reference` is available. The search provider sits behind the `ReferenceSearcher` port, so it can be replaced without changing the agent.
