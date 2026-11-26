# LLM API usage and cost tracking

Every model call made by the coding agent is captured and priced. The worker prints the cost in the terminal when an attempt finishes or crashes, and prints a session summary when it stops. The amounts are **LLM API cost only**: Docker, the database, GitHub, Jira and servers are not measured.

## Provider and model

| Setting | Value used for KAN-7 |
|---|---|
| `LLM_PROVIDER` | `anthropic` (`ChatAnthropic`, langchain-anthropic 1.7.12, anthropic SDK 2.14.0) |
| `LLM_MODEL` | `claude-haiku-5-5` |

On Haiku 5.5, adaptive thinking is on by default and effort defaults to `medium`. The factory sends no `thinking`, `temperature` or `top_p` parameters. Deep Agents adds Anthropic prompt caching on its own, so cache writes and cache reads appear in the usage.

**Escalation to Sonnet is not implemented.** There is no `LLM_FALLBACK_*` setting. Haiku completed the compatibility test and KAN-7 without one. The pricing table already knows `claude-sonnet-5-5`. A switch would therefore show up in the per-stage "models used", and its cost would be priced correctly.

## Compatibility test

`var/s2044_deepagent_compat.py` is run by hand; it makes real model calls. It runs the production `CodingAgent` with the configured model against a real `DockerSandbox` on a throwaway workspace:

- Planning uses the read-only sandbox and returns a structured `ImplementationPlan`.
- Implementation uses the writable sandbox, edits files, runs commands and returns a structured `ImplementationReport`.
- Every model call's usage is captured.

## Capture

- **Hook:** `application/costs/usage.py`. `UsageTracker.handlers(thread_id)` gives each agent phase run a LangChain callback handler, passed in the run config.
- **Usage source:** for each chat-model call, `on_llm_end` reads the provider's own `usage` block from `response_metadata`:
  - `input_tokens` (uncached);
  - `output_tokens`, including thinking;
  - `cache_creation` (`ephemeral_5m` / `ephemeral_1h`);
  - `cache_read_input_tokens`;
  - `inference_geo`.
- **Fallback:** if that block is missing, LangChain's normalised `usage_metadata` is used. Cache reads and writes are subtracted from its `input_tokens`, because LangChain includes them there.
- **Failed calls:** `on_llm_error` records the call with its usage marked unavailable. Its cost is unknown, never zero.
- **Stage:** taken from the thread id:
  - `planning`: repository investigation and the plan;
  - `implementation`: coding, tests and the final structured report;
  - `correction`: an implementation run after failed validation.
- **Fields recorded:** call id (LangChain run id), provider message id (`msg_...`), session id, attempt id, stage, provider, model, success, the four token categories, cost, long-context flag, pricing version and timestamp.
- **Jira issue and repository:** not stored on the record. They are joined from the attempt when a report is built.

### Persistence and idempotency

- **Table:** `llm_usage_records`, created by migration `0002`. It is append-only: a trigger rejects `UPDATE` and `DELETE`.
- **Unique keys:** the call id (primary key) and the provider message id. A replayed insert or a replayed response is therefore stored once.
- **Retries:**
  - A LangGraph node that really re-runs after a crash makes new provider calls. These are billed, so they are recorded.
  - Retries inside the Anthropic SDK (`LLM_MAX_RETRIES`) are not visible to the callback. Only the final response is recorded, so a retried request that was billed is missing from the totals.
- **Recording failures:** a failure to record is logged and never fails the workflow.

## Pricing

`application/costs/pricing.py` is the only place that knows a rate. It uses `PRICING_VERSION = anthropic-api-2026-11-25`, from https://platform.claude.com/docs/en/about-claude/pricing, checked on 2026-11-25.

| Model (USD / MTok) | Input | Output | 5m cache write | 1h cache write | Cache read |
|---|---:|---:|---:|---:|---:|
| claude-haiku-5-5, prompt ≤ 100K | 0.10 | 0.50 | 0.125 | 0.20 | 0.01 |
| claude-haiku-5-5, prompt > 100K | 0.50 | 2.50 | 0.625 | 1.00 | 0.05 |
| claude-sonnet-5-5 | 2.00 | 10.00 | 2.50 | 4.00 | 0.10 |

How a call is priced:

```
call_cost = input x input_rate + output x output_rate + cache_write_5m x w5 + cache_write_1h x w1 + cache_read x read
```

- The provider reports the categories without overlap, so nothing is counted twice.
- A Haiku prompt is measured as input + cache writes + cache reads. Over 100,000 tokens, every category of that request pays the long-context rate.
- `inference_geo: "us"` multiplies the cost by 1.1.
- A model without a configured price is reported as unknown cost.
- To override the rates, set `LLM_PRICING_FILE` to an absolute path to a JSON price list. The format is in `PriceTable.from_file`.

## Reports

- **When an attempt reaches a terminal state, or its run raises:** the worker prints a TASK COST REPORT with:
  - the attempt's tokens;
  - cost by stage, with the model(s) used in each stage;
  - the attempt total;
  - the task total over all attempts that have usage records.
- **When the worker stops (Ctrl-C/SIGTERM or `--once`):** it prints an EXECUTION SESSION COST SUMMARY covering every task this session spent money on, including failed ones. It shows:
  - completed, failed and other tasks;
  - total attempts;
  - per task: this session's cost and the cumulative task cost;
  - per attempt: its cost;
  - the session total.
- **Source of the numbers:** both reports are computed from the database, so a restarted worker reports correct cumulative totals.
- **On demand:** `uv run ai-code-change costs --issue KAN-7 [--attempt ID] [--session ID]` prints the same reports.
- **Incomplete costs:** missing usage is printed as `at least $X (INCOMPLETE: n call(s) without usage)`, never as zero.

## Bedrock (staging and production)

- **Pricing:** Bedrock is partner-priced, and its model ids (`anthropic.…`, inference profiles) are not in the built-in table. Until you set `LLM_PRICING_FILE` with your Bedrock rates and model ids, Bedrock calls are reported as **unknown cost**, never $0.
- **Usage capture:** comes from LangChain's normalised `usage_metadata`, the fallback path described under Capture.

## Limitations

- **Before 2026-11-25:** attempts 1-7 of KAN-7 ran before this ledger existed. Their cost is not known and is not included.
- **SDK-internal retries:** see Persistence and idempotency.
- **Deep Agents summarisation:** these calls go through the same model and the same callbacks, so they are recorded under the phase that triggered them.

## Verified run: KAN-7, 2026-11-25

| Attempt | Result | Calls | Input / output / cache write / cache read | Cost by stage | Attempt cost |
|---|---|---:|---|---|---:|
| 8 | REJECTED by the PLAN policy (`.gitignore` is protected) | 8 | 98 / 10,035 / 17,905 / 76,109 | planning $0.0080 | $0.0080 |
| 9 | COMPLETED, draft PR #2 | 18 | 222 / 14,739 / 31,490 / 215,394 | planning $0.0024, implementation $0.0061, correction $0.0050 | $0.0135 |

- **Task total:** $0.0215 USD (LLM API only; attempts 1-7 predate the ledger).
- **Model:** every call used `claude-haiku-5-5`; no escalation.
- **Pricing:** no call crossed the 100K long-context threshold.

## Runaway-cost guards (any model)

On KAN-9 the model kept sending one field in the wrong shape, and the SDK retried the structured answer 176 times ($0.40). These guards apply to every model (Haiku, Sonnet, Opus, Bedrock):

| Guard | Effect |
|---|---|
| Tolerant list fields | Lists sent as a JSON string or plain text are accepted (the observed cause) |
| Structured-output retries | 3 consecutive parse failures end the phase as `agent_output_invalid` |
| `DEEP_AGENT_MAX_MODEL_CALLS` (80) | LangChain `ModelCallLimitMiddleware` per phase run → `agent_model_call_limit` |
| `LLM_MAX_COST_PER_PHASE_USD` (2.0) | Stops before the next model call once the phase run has spent its budget → `agent_cost_budget_exceeded` |

- **Worst case per attempt:** an attempt has at most planning, implementation and `MAX_REPAIR_ATTEMPTS` correction phases, so it costs at most (2 + MAX_REPAIR_ATTEMPTS) × the phase budget.
- **Worst case per Story:** a multi-repository Story costs at most that amount per child.
- **No automatic retry:** a guard-stopped attempt is FAILED, never re-run automatically.

**Prices.** Providers return token counts only, never dollars. Opus 5.5 is now in the built-in table. Bedrock bills at its own (partner) prices:
- **Reports** show Bedrock calls as unknown cost unless `LLM_PRICING_FILE` prices the Bedrock model id.
- **The budget guard** still uses the matching Claude list price.
- **A warning is logged at start-up** when `LLM_MODEL` has no configured price.

Documentation searches are recorded as stage `research`: tokens plus $0.01 per search.
