# ADR-011: Harness-side reference fetching for the coding agent

- **Status:** Accepted (owner request) and implemented.
- **Date:** 2026-11-25
- **Amends:** C2 (agent tools) and the `tools=[]` guard in `tests/unit/test_architecture.py`.

## Context

Stories link documentation (for example `https://docs.langchain.com/llms.txt`) that the agent needs for context. The sandbox has no network and must keep none. Claude Code solves the same problem with a web-fetch tool that runs in the harness under permission rules.

## Decision

The Deep Agent gets exactly one custom tool, `fetch_reference(url)`:

- **Runs in the worker** (`integrations/web/fetcher.py` behind `application/references/ports.py`), never in the sandbox. The agent module imports no HTTP client; composition injects the tool per phase (`CodingAgent(tools=factory)`).
- **Permission rules, enforced in code:**
  - https only, default port, no credentials in the URL;
  - the host must exactly equal a host listed in `config/reference_sources.yaml` (case-insensitive, trailing dot ignored, IDNA compared in punycode). Every path on a listed host is allowed; subdomains are never implied (amended 2026-11-27: the earlier rule also trusted subdomains);
  - every resolved address must be public (no private, loopback, link-local, metadata or multicast ranges);
  - at most 3 redirects, each re-checked;
  - GET only, no cookies or authorization headers, `trust_env=False`.
- **Content:**
  - text types only, a `REFERENCE_MAX_BYTES` cap, HTML reduced to text (scripts and styles dropped, links kept);
  - returned between `UNTRUSTED DATA BEGINS/ENDS` markers, and the prompts tell the agent never to follow instructions in fetched text.
- **Budget and audit:**
  - `REFERENCE_MAX_FETCHES` per agent phase;
  - every fetch is logged (host, status, size, truncation);
  - refusals return a fixed `REFUSED: <code>` string to the agent.
- A missing `config/reference_sources.yaml` (or an empty `allowed_hosts`) disables the tools; an invalid file fails startup.

## Security implications

- New outbound traffic comes from the worker, only to the allow-listed documentation sites.
- A residual exfiltration channel exists: the URL path requested from an allow-listed site, which is logged. Keep the list to documentation hosts you trust.
- Prompt injection through fetched text is treated like Story text: quoted as untrusted data. Its effects are bounded by the offline sandbox, automatic policy gates and independent validation.
- There's a short DNS-rebinding window between the address check and the connection; the allow-list limits it to listed sites.

## Alternatives

- Network in the sandbox: rejected (C2).
- Pre-fetching Story links only: simpler, but the agent can't follow links inside an `llms.txt`. It may be added later on top of this tool.
'

## Verification (2026-11-26)

**Agent-driven use, observed (not a manual tool call).** The production planning agent (Deep Agent, `claude-haiku-5-5`, tools from `build_reference_tools`, offline read-only sandbox over the `automate_connector` KAN-7 branch) was given a Story whose answer depends on current FastAPI docs. Both tools were registered (`search_docs`, `fetch_reference`) and the agent itself called `search_docs` (8 allow-listed hits, `fastapi.tiangolo.com/advanced/events/` first), then `fetch_reference` on that page, and its plan chose `lifespan=` over the deprecated `@app.on_event`, citing the URL with retrieval time `2026-11-26T12:33:23Z`. Evidence: `agent_tool_call` structured log events (tool, outcome, URLs, retrieved_at, thread id) and 9 ledger rows for the session (research search $0.0124; total $0.017). Reproduce with `scripts/verify_research_agent.py` (real model calls).

**Official sources fetched live** through `HttpReferenceFetcher`: docs.python.org, fastapi.tiangolo.com, docs.langchain.com (Deep Agents), platform.claude.com, docs.aws.amazon.com, www.postgresql.org, docs.sqlalchemy.org, docs.docker.com, docs.github.com, developer.atlassian.com (Jira REST v3), docs.astral.sh (uv), docs.pytest.org. Refused live: plain http, non-default port, credentials in URL, IP literals (127.0.0.1, 169.254.169.254), localhost, unlisted and look-alike domains (`docs.python.org.evil.com`).

**Fixes made during verification.**
- HTML pages are streamed through the text extractor (raw HTML bounded at 8x `REFERENCE_MAX_BYTES`, extracted text at 40,000 chars). Before, the Jira REST v3 page (3 MB, `<body>` at 2.7 MB) returned 0 characters.
- Search costs now count against the per-phase cost guard (`LLM_MAX_COST_PER_PHASE_USD`): an unpriced search model is charged at the highest configured rate for the guard (the ledger keeps the real price or none), and `search_docs` refuses further searches once the phase budget is spent. Before, searches were only written to the ledger.
- A search the provider ran but answered with an error is now recorded (billed) and refused to the agent; before, it was not recorded.
- Every tool invocation (allowed or refused) is logged as `agent_tool_call`; fetched documents carry `retrieved_at`.

**Limits.** `search_docs` exists only with the Anthropic provider (server-side web search); with Bedrock the agent has `fetch_reference` only. The version-controlled `config/reference_sources.yaml` decides which hosts are available; a redirect is followed only when its target host is listed too (for example `docs.pydantic.dev` → `pydantic.dev` needs both).
'
