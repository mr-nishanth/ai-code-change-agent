# ADR-012: LLM usage and cost ledger

Status: accepted (2026-11-25, S2044)

## Context

The owner needs the actual Anthropic API cost of each Jira task, of each attempt and of each worker session. That cost must survive restarts and must not be estimated.

## Decision

- **Capture:** a LangChain callback handler is attached to every coding-agent phase run, through a factory injected into `CodingAgent`. It reads the provider-reported usage of every chat-model call.
- **Pricing:** one versioned price table (`application/costs/pricing.py`, overridable with `LLM_PRICING_FILE`).
- **Storage:** each call becomes one append-only row in `llm_usage_records`.
  - The call id and the provider message id are unique, so a replay is not counted twice.
  - Missing usage is stored as NULL and reported as unknown, never as zero.
- **Reports:** the worker prints task reports and a session summary, computed from the database.
- **Scope:**
  - No new agent, service, queue or model router.
  - The agent package stays free of persistence imports: it only receives callbacks.

## Consequences

- **Gaps:** a call that the Anthropic SDK retries internally can be billed without being recorded (see docs/LLM-COST-TRACKING.md).
- **Escalation to Sonnet is deferred:** Haiku 5.5 completed the work. If escalation is added later, the ledger already prices Sonnet 5.5 and records the model per call.
