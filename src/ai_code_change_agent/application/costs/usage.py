"""Capture of every model call the coding agent makes.

A LangChain callback handler is attached to each agent phase run. For every chat-model call that
reaches its end it reads the provider's own usage block (Anthropic: `input_tokens`,
`output_tokens`, `cache_creation` split by TTL, `cache_read_input_tokens`), prices it and
hands one record to the store. A call that fails is recorded too, with its usage marked
unavailable: it is reported as an unknown cost, never as zero.

Idempotency: the record id is the LangChain run id of that model call and the provider message id
is unique, so a replayed write (a retried insert, a resumed checkpoint) records nothing new. A
node that really re-runs after a crash makes new provider calls; those are billed, so they are
recorded as new calls.

Recording never breaks the workflow: a store failure is logged and the call is reported as
missing from the totals."""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult

from ai_code_change_agent.application.costs.pricing import (
    WEB_SEARCH_USD_PER_REQUEST,
    PriceTable,
    TokenUsage,
)

log = logging.getLogger(__name__)

STAGE_PLANNING = "planning"
STAGE_IMPLEMENTATION = "implementation"
STAGE_CORRECTION = "correction"


@dataclass(frozen=True)
class LlmCallRecord:
    call_id: uuid.UUID
    session_id: uuid.UUID
    attempt_id: uuid.UUID | None
    stage: str
    provider: str
    model: str
    succeeded: bool
    usage: TokenUsage | None  # None: the provider reported no usage (cost unknown)
    cost_usd: Decimal | None  # None: usage missing or the model has no configured price
    pricing_version: str
    provider_message_id: str | None = None
    long_context: bool = False
    inference_geo: str | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class UsageStore(Protocol):
    async def add(self, record: LlmCallRecord) -> bool:
        """True when stored; False when this call (id or provider message id) already exists."""
        ...


@dataclass(frozen=True)
class ReportedUsage:
    usage: TokenUsage | None
    model: str | None
    message_id: str | None
    inference_geo: str | None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def extract_usage(message: BaseMessage) -> ReportedUsage:
    """The provider's own token counts for one response; usage None when it reported none."""
    meta: dict[str, Any] = dict(getattr(message, "response_metadata", None) or {})
    model = meta.get("model") or meta.get("model_name")
    message_id = meta.get("id") if isinstance(meta.get("id"), str) else None
    raw = meta.get("usage")
    if isinstance(raw, dict):
        geo = raw.get("inference_geo") if isinstance(raw.get("inference_geo"), str) else None
        inp, out = _int(raw.get("input_tokens")), _int(raw.get("output_tokens"))
        if inp is not None and out is not None:
            created = _int(raw.get("cache_creation_input_tokens")) or 0
            split = raw.get("cache_creation")
            w5 = _int(split.get("ephemeral_5m_input_tokens")) if isinstance(split, dict) else None
            w1 = _int(split.get("ephemeral_1h_input_tokens")) if isinstance(split, dict) else None
            if w5 is None and w1 is None:
                w5, w1 = created, 0  # no TTL split reported: the default 5-minute cache
            usage = TokenUsage(
                input_tokens=inp,
                output_tokens=out,
                cache_write_5m_tokens=w5 or 0,
                cache_write_1h_tokens=w1 or 0,
                cache_read_tokens=_int(raw.get("cache_read_input_tokens")) or 0,
            )
            return ReportedUsage(usage, model, message_id, geo)
    # LangChain's normalised usage: its input_tokens INCLUDES cache reads and cache writes.
    norm = getattr(message, "usage_metadata", None)
    if isinstance(norm, dict):
        inp, out = _int(norm.get("input_tokens")), _int(norm.get("output_tokens"))
        if inp is not None and out is not None:
            details = norm.get("input_token_details") or {}
            read = _int(details.get("cache_read")) or 0
            w5 = _int(details.get("ephemeral_5m_input_tokens"))
            w1 = _int(details.get("ephemeral_1h_input_tokens"))
            created = _int(details.get("cache_creation")) or 0
            if w5 is None and w1 is None:
                w5, w1 = created, 0
            writes = (w5 or 0) + (w1 or 0)
            usage = TokenUsage(max(inp - read - writes, 0), out, w5 or 0, w1 or 0, read)
            return ReportedUsage(usage, model, message_id, None)
    return ReportedUsage(None, model, message_id, None)


def stage_of(thread_id: str) -> tuple[uuid.UUID | None, str]:
    """attempt:<id>:planning:<run> | attempt:<id>:implementation:<repairs>:<run>."""
    parts = thread_id.split(":")
    attempt: uuid.UUID | None = None
    if len(parts) >= 3 and parts[0] == "attempt":
        try:
            attempt = uuid.UUID(parts[1])
        except ValueError:
            attempt = None
    phase = parts[2] if len(parts) >= 3 else "agent"
    if phase == STAGE_IMPLEMENTATION and len(parts) >= 4 and parts[3].isdigit() and int(parts[3]):
        return attempt, STAGE_CORRECTION  # a bounded repair run after failed validation
    return attempt, phase


class UsageCallbackHandler(AsyncCallbackHandler):
    """One record per chat-model call of one agent phase run."""

    raise_error = False
    run_inline = True

    def __init__(
        self,
        store: UsageStore,
        prices: PriceTable,
        *,
        session_id: uuid.UUID,
        attempt_id: uuid.UUID | None,
        stage: str,
        provider: str,
        model: str,
    ) -> None:
        self._store = store
        self._prices = prices
        self._session = session_id
        self._attempt = attempt_id
        self._stage = stage
        self._provider = provider
        self._model = model
        self._seen: set[uuid.UUID] = set()

    async def on_llm_end(self, response: LLMResult, *, run_id: uuid.UUID, **_: Any) -> None:
        message: BaseMessage | None = next(
            (
                m
                for batch in response.generations
                for g in batch
                if isinstance(m := getattr(g, "message", None), BaseMessage)
            ),
            None,
        )
        reported = (
            extract_usage(message) if message is not None else ReportedUsage(None, None, None, None)
        )
        model = reported.model or self._model
        cost = (
            self._prices.price(model, reported.usage, inference_geo=reported.inference_geo)
            if reported.usage is not None
            else None
        )
        await self._save(
            LlmCallRecord(
                call_id=run_id,
                session_id=self._session,
                attempt_id=self._attempt,
                stage=self._stage,
                provider=self._provider,
                model=model,
                succeeded=True,
                usage=reported.usage,
                cost_usd=cost.total if cost is not None else None,
                pricing_version=self._prices.version,
                provider_message_id=reported.message_id,
                long_context=cost.long_context if cost is not None else False,
                inference_geo=reported.inference_geo,
            )
        )

    async def on_llm_error(self, error: BaseException, *, run_id: uuid.UUID, **_: Any) -> None:
        await self._save(
            LlmCallRecord(
                call_id=run_id,
                session_id=self._session,
                attempt_id=self._attempt,
                stage=self._stage,
                provider=self._provider,
                model=self._model,
                succeeded=False,
                usage=None,  # a failed call reports no usage: unknown, not zero
                cost_usd=None,
                pricing_version=self._prices.version,
                error_code=type(error).__name__[:64],
            )
        )

    async def _save(self, record: LlmCallRecord) -> None:
        if record.call_id in self._seen:
            return
        self._seen.add(record.call_id)
        try:
            await self._store.add(record)
        except Exception:  # recording must never break the coding workflow
            log.warning("llm_usage_record_failed", extra={"stage": record.stage}, exc_info=True)


class BudgetExceededError(RuntimeError):
    code = "agent_cost_budget_exceeded"


class BudgetGuard(AsyncCallbackHandler):
    """Stops a phase run before its next model call once its priced cost reaches the budget.
    Works for any model: a call whose model has no price is charged at the most expensive
    configured rate (never as zero), and the model-call cap still applies on top."""

    raise_error = True
    run_inline = True

    def __init__(self, prices: PriceTable, budget_usd: Decimal) -> None:
        self._prices = prices
        self._budget = budget_usd
        self.budget = budget_usd
        self.spent = Decimal(0)
        self.unpriced_calls = 0

    async def on_chat_model_start(self, serialized: Any, messages: Any, **_: Any) -> None:
        if self.spent >= self._budget:
            raise BudgetExceededError(f"phase cost {self.spent:.4f} USD >= {self._budget} USD")

    async def on_llm_end(self, response: LLMResult, **_: Any) -> None:
        for batch in response.generations:
            for g in batch:
                message = getattr(g, "message", None)
                if isinstance(message, BaseMessage):
                    reported = extract_usage(message)
                    if reported.usage is not None and reported.model:
                        cost = self._prices.estimate(  # Bedrock ids: list-price estimate
                            reported.model, reported.usage, inference_geo=reported.inference_geo
                        )
                        if cost is None:
                            self.unpriced_calls += 1
                            cost = self._prices.conservative(reported.usage)
                        if cost is not None:
                            self.spent += cost.total
                    return


class UsageTracker:
    """Builds the callback handler for one agent phase run (passed to the coding agent)."""

    def __init__(
        self,
        store: UsageStore,
        prices: PriceTable,
        *,
        provider: str,
        model: str,
        session_id: uuid.UUID | None = None,
        phase_budget_usd: Decimal | None = None,
    ) -> None:
        self.phase_budget_usd = phase_budget_usd
        self.store = store
        self.prices = prices
        self.provider = provider
        self.model = model
        self.session_id = session_id or uuid.uuid4()
        self._guards: dict[str, BudgetGuard] = {}  # per phase run (thread id)

    def search_allowed(self, thread_id: str) -> bool:
        """False once the phase run's budget is spent: searches are refused like model calls."""
        guard = self._guards.get(thread_id)
        return guard is None or guard.spent < guard.budget

    async def record_search(self, thread_id: str, result: Any) -> None:
        """A documentation search made by the harness (Anthropic web search): its tokens plus the
        per-search fee, recorded in the same ledger under the "research" stage."""
        attempt, _stage = stage_of(thread_id)
        usage = (
            TokenUsage(result.input_tokens, result.output_tokens)
            if result.input_tokens is not None and result.output_tokens is not None
            else None
        )
        fee = WEB_SEARCH_USD_PER_REQUEST * result.searches
        cost = None  # ledger: only a real price is recorded
        guard_cost = fee  # guard: never free; unpriced models charged at the highest rate
        if usage is not None:
            priced = self.prices.price(result.model, usage) if result.model else None
            if priced is not None:
                cost = priced.total + fee
                guard_cost = cost
            elif (worst := self.prices.conservative(usage)) is not None:
                guard_cost = worst.total + fee
        guard = self._guards.get(thread_id)
        if guard is not None:
            guard.spent += guard_cost  # searches count against the phase budget
        record = LlmCallRecord(
            call_id=uuid.uuid4(),
            session_id=self.session_id,
            attempt_id=attempt,
            stage="research",
            provider="anthropic",
            model=result.model or self.model,
            succeeded=getattr(result, "error", None) is None,
            usage=usage,
            cost_usd=cost,
            pricing_version=self.prices.version,
        )
        try:
            await self.store.add(record)
        except Exception:  # recording must never break the coding workflow
            log.warning("llm_usage_record_failed", extra={"stage": "research"}, exc_info=True)

    def handlers(self, thread_id: str) -> Sequence[BaseCallbackHandler]:
        attempt, stage = stage_of(thread_id)
        handlers: list[BaseCallbackHandler] = [
            UsageCallbackHandler(
                self.store,
                self.prices,
                session_id=self.session_id,
                attempt_id=attempt,
                stage=stage,
                provider=self.provider,
                model=self.model,
            )
        ]
        if self.phase_budget_usd:
            guard = BudgetGuard(self.prices, self.phase_budget_usd)
            self._guards[thread_id] = guard
            # Last: langchain creates every handler's coroutine before awaiting them in order, so
            # any handler after a raising guard would never be awaited (RuntimeWarning).
            handlers.append(guard)
        return handlers


CallbackFactory = Callable[[str], Sequence[BaseCallbackHandler]]
