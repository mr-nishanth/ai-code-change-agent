# ADR-011: Harness-side reference fetching for the coding agent

- **Status:** Accepted (owner request, S2043) and implemented.
- **Date:** 2026-11-25
- **Amends:** C2 (agent tools) and the `tools=[]` guard in `tests/unit/test_architecture.py`.

## Context

Stories link documentation (for example `https://docs.langchain.com/llms.txt`) that the agent needs for context. The sandbox has no network and must keep none. Claude Code solves the same problem with a web-fetch tool that runs in the harness under permission rules.

## Decision

The Deep Agent gets exactly one custom tool, `fetch_reference(url)`:

- **Runs in the worker** (`integrations/web/fetcher.py` behind `application/references/ports.py`), never in the sandbox. The agent module imports no HTTP client; composition injects the tool per phase (`CodingAgent(tools=factory)`).
- **Permission rules, enforced in code:**
  - https only, default port, no credentials in the URL;
  - the host must match `REFERENCE_ALLOWED_DOMAINS` (exact domain or subdomain);
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
- An empty allow-list disables the tool.

## Security implications

- New outbound traffic comes from the worker, only to the allow-listed documentation sites.
- A residual exfiltration channel exists: the URL path requested from an allow-listed site, which is logged. Keep the list to documentation hosts you trust.
- Prompt injection through fetched text is treated like Story text: quoted as untrusted data. Its effects are bounded by the offline sandbox, automatic policy gates and independent validation.
- There's a short DNS-rebinding window between the address check and the connection; the allow-list limits it to listed sites.

## Alternatives

- Network in the sandbox: rejected (C2).
- Pre-fetching Story links only: simpler, but the agent can't follow links inside an `llms.txt`. It may be added later on top of this tool.
