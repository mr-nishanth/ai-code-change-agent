"""LLM usage capture, pricing, persistence semantics and the terminal cost reports."""

import json
import uuid
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from ai_code_change_agent.application.costs.pricing import PriceTable, TokenUsage
from ai_code_change_agent.application.costs.report import (
    CostRow,
    session_summary,
    task_report,
    totals,
)
from ai_code_change_agent.application.costs.service import CostReporter
from ai_code_change_agent.application.costs.usage import (
    LlmCallRecord,
    UsageCallbackHandler,
    UsageTracker,
    extract_usage,
    stage_of,
)

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
D = Decimal


def anthropic_message(
    inp: int,
    out: int,
    *,
    w5: int = 0,
    w1: int = 0,
    read: int = 0,
    model: str = HAIKU,
    msg_id: str | None = None,
    geo: str = "global",
) -> AIMessage:
    return AIMessage(
        content="ok",
        response_metadata={
            "id": msg_id or f"msg_{uuid.uuid4().hex}",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_creation_input_tokens": w5 + w1,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": w5,
                    "ephemeral_1h_input_tokens": w1,
                },
                "cache_read_input_tokens": read,
                "inference_geo": geo,
            },
        },
    )


def result(message: AIMessage) -> LLMResult:
    return LLMResult(generations=[[ChatGeneration(message=message)]])


class MemoryStore:
    """Same idempotency as the PostgreSQL ledger: call id and provider message id are unique."""

    def __init__(self) -> None:
        self.records: list[LlmCallRecord] = []

    async def add(self, record: LlmCallRecord) -> bool:
        if any(
            r.call_id == record.call_id
            or (record.provider_message_id and r.provider_message_id == record.provider_message_id)
            for r in self.records
        ):
            return False
        self.records.append(record)
        return True


class MemoryQuery:
    def __init__(self, store: MemoryStore, attempts: dict[uuid.UUID, tuple[str, int, str]]) -> None:
        self.store = store
        self.attempts = attempts  # attempt id -> (issue key, number, status)

    async def rows(
        self,
        *,
        attempt_ids: Sequence[uuid.UUID] | None = None,
        issue_keys: Sequence[str] | None = None,
        session_id: uuid.UUID | None = None,
    ) -> list[CostRow]:
        out = []
        for r in self.store.records:
            issue, number, status = self.attempts.get(r.attempt_id, (None, None, None))  # type: ignore[arg-type]
            if attempt_ids is not None and r.attempt_id not in attempt_ids:
                continue
            if issue_keys is not None and issue not in issue_keys:
                continue
            if session_id is not None and r.session_id != session_id:
                continue
            out.append(CostRow(r, issue, number, status, "shop-service"))
        return out


def handler(
    store: MemoryStore, attempt: uuid.UUID | None, stage: str, session: uuid.UUID
) -> UsageCallbackHandler:
    return UsageCallbackHandler(
        store,
        PriceTable(),
        session_id=session,
        attempt_id=attempt,
        stage=stage,
        provider="anthropic",
        model=HAIKU,
    )


# ---- 1-5: usage extraction and token categories -------------------------------------------


def test_anthropic_usage_is_read_from_the_provider_usage_block() -> None:
    reported = extract_usage(anthropic_message(120, 45, w5=300, w1=7, read=900, msg_id="msg_1"))
    assert reported.usage == TokenUsage(120, 45, 300, 7, 900)
    assert reported.model == HAIKU and reported.message_id == "msg_1"
    assert reported.usage.prompt_tokens == 120 + 307 + 900


def test_langchain_normalised_usage_does_not_double_count_cached_tokens() -> None:
    # LangChain's input_tokens includes cache reads and writes; the uncached part is the rest.
    msg = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 10,
            "total_tokens": 1010,
            "input_token_details": {"cache_read": 600, "cache_creation": 300},
        },
    )
    assert extract_usage(msg).usage == TokenUsage(100, 10, 300, 0, 600)


def test_missing_usage_is_reported_as_missing_not_zero() -> None:
    assert extract_usage(AIMessage(content="x")).usage is None


def test_input_output_cache_read_and_cache_write_costs_for_haiku() -> None:
    cost = PriceTable().price(HAIKU, TokenUsage(100_000, 0))
    assert cost is not None and cost.total == D("0.01")  # $0.10 / MTok
    assert PriceTable().price(HAIKU, TokenUsage(0, 1_000_000)).total == D("0.50")  # type: ignore[union-attr]
    cached = PriceTable().price(HAIKU, TokenUsage(0, 0, cache_read_tokens=100_000))
    assert cached is not None and cached.cache_read == D("0.001")  # $0.01 / MTok
    writes = PriceTable().price(
        HAIKU, TokenUsage(0, 0, cache_write_5m_tokens=40_000, cache_write_1h_tokens=40_000)
    )
    assert writes is not None
    assert writes.cache_write == D("0.005") + D("0.008")  # 0.125/MTok and 0.20/MTok


# ---- 6-8: model pricing -----------------------------------------------------------------


def test_haiku_standard_pricing_at_the_100k_boundary() -> None:
    cost = PriceTable().price(HAIKU, TokenUsage(100_000, 10_000))
    assert cost is not None and not cost.long_context
    assert cost.total == D("0.01") + D("0.005")


def test_haiku_long_prompt_pricing_counts_cache_reads_and_writes_in_the_prompt() -> None:
    usage = TokenUsage(10_000, 2_000, cache_write_5m_tokens=1_000, cache_read_tokens=90_000)
    cost = PriceTable().price(HAIKU, usage)
    assert cost is not None and cost.long_context  # 101,000 prompt tokens
    assert cost.input == D("0.005")  # 10k x $0.50
    assert cost.output == D("0.005")  # 2k x $2.50
    assert cost.cache_write == D("0.000625")
    assert cost.cache_read == D("0.0045")  # 90k x $0.05


def test_sonnet_pricing_and_unknown_models() -> None:
    cost = PriceTable().price(SONNET, TokenUsage(1_000_000, 1_000_000, cache_read_tokens=1_000_000))
    assert cost is not None and cost.total == D("2") + D("10") + D("0.10")
    assert PriceTable().price("some-other-model", TokenUsage(1, 1)) is None  # unknown, not zero


def test_us_inference_geo_multiplier_and_price_file_override(tmp_path: Path) -> None:
    us = PriceTable().price(HAIKU, TokenUsage(100_000, 0), inference_geo="us")
    assert us is not None and us.total == D("0.011")
    doc = {
        "version": "test-prices",
        "models": {
            "m": {
                "input": "1",
                "output": "2",
                "cache_write_5m": "1.25",
                "cache_write_1h": "2",
                "cache_read": "0.1",
            }
        },
    }
    path = tmp_path / "prices.json"
    path.write_text(json.dumps(doc))
    table = PriceTable.from_file(path)
    assert table.version == "test-prices"
    assert table.price("m", TokenUsage(1_000_000, 1_000_000)).total == D("3")  # type: ignore[union-attr]


# ---- 9-15: recording, aggregation and reports ---------------------------------------------


async def test_every_call_is_recorded_once_with_stage_and_model() -> None:
    store, session, attempt = MemoryStore(), uuid.uuid4(), uuid.uuid4()
    h = handler(store, attempt, "planning", session)
    run = uuid.uuid4()
    msg = anthropic_message(10, 5, msg_id="msg_a")
    await h.on_llm_end(result(msg), run_id=run)
    await h.on_llm_end(result(msg), run_id=run)  # the same callback twice
    assert len(store.records) == 1
    rec = store.records[0]
    assert (rec.stage, rec.model, rec.attempt_id, rec.session_id) == (
        "planning",
        HAIKU,
        attempt,
        session,
    )
    assert rec.cost_usd == D(10) * D("0.10") / D(1_000_000) + D(5) * D("0.50") / D(1_000_000)


async def test_duplicate_checkpoint_replay_does_not_double_count() -> None:
    store, session, attempt = MemoryStore(), uuid.uuid4(), uuid.uuid4()
    msg = anthropic_message(10, 5, msg_id="msg_replayed")
    await handler(store, attempt, "planning", session).on_llm_end(result(msg), run_id=uuid.uuid4())
    # a replay surfaces the same provider response under a new run id and a new handler
    await handler(store, attempt, "planning", session).on_llm_end(result(msg), run_id=uuid.uuid4())
    assert len(store.records) == 1


async def test_failed_calls_are_recorded_with_unknown_cost() -> None:
    store, session, attempt = MemoryStore(), uuid.uuid4(), uuid.uuid4()
    await handler(store, attempt, "implementation", session).on_llm_error(
        TimeoutError("read timeout"), run_id=uuid.uuid4()
    )
    rec = store.records[0]
    assert not rec.succeeded and rec.usage is None and rec.cost_usd is None
    assert rec.error_code == "TimeoutError"
    t = totals(store.records)
    assert not t.complete and "INCOMPLETE" in t.money()


async def test_a_store_failure_never_breaks_the_agent() -> None:
    class Broken:
        async def add(self, record: LlmCallRecord) -> bool:
            raise RuntimeError("db down")

    h = UsageCallbackHandler(
        Broken(), PriceTable(), session_id=uuid.uuid4(), attempt_id=None, stage="planning",
        provider="anthropic", model=HAIKU,
    )  # fmt: skip
    await h.on_llm_end(result(anthropic_message(1, 1)), run_id=uuid.uuid4())


def test_thread_ids_map_to_stages() -> None:
    a = uuid.uuid4()
    assert stage_of(f"attempt:{a}:planning:abcd1234") == (a, "planning")
    assert stage_of(f"attempt:{a}:implementation:0:abcd1234") == (a, "implementation")
    assert stage_of(f"attempt:{a}:implementation:1:abcd1234") == (a, "correction")
    assert stage_of("t-plan") == (None, "agent")
    handlers = UsageTracker(
        MemoryStore(), PriceTable(), provider="anthropic", model=HAIKU
    ).handlers(f"attempt:{a}:planning:x")
    assert len(handlers) == 1


async def _scenario() -> tuple[
    MemoryStore, MemoryQuery, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID
]:
    """SHOP-7: attempt 1 failed (session 1), attempt 2 completed using Haiku and Sonnet (session 2);
    SHOP-8 failed in session 2."""
    store = MemoryStore()
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    a1, a2, b1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    query = MemoryQuery(
        store,
        {a1: ("SHOP-7", 1, "FAILED"), a2: ("SHOP-7", 2, "COMPLETED"), b1: ("SHOP-8", 1, "FAILED")},
    )
    await handler(store, a1, "planning", s1).on_llm_end(
        result(anthropic_message(100_000, 0)), run_id=uuid.uuid4()
    )  # $0.01
    await handler(store, a2, "planning", s2).on_llm_end(
        result(anthropic_message(0, 1_000_000)), run_id=uuid.uuid4()
    )  # $0.50
    await handler(store, a2, "correction", s2).on_llm_end(
        result(anthropic_message(100_000, 0, model=SONNET)), run_id=uuid.uuid4()
    )  # $0.20
    await handler(store, b1, "planning", s2).on_llm_end(
        result(anthropic_message(0, 100_000)), run_id=uuid.uuid4()
    )  # $0.05
    return store, query, s1, s2, a2, b1


async def test_multiple_models_and_attempts_aggregate_per_attempt_and_per_task() -> None:
    _store, query, _, _, a2, _ = await _scenario()
    rows = await query.rows(issue_keys=["SHOP-7"])
    text = task_report(
        a2, rows, provider="anthropic", primary_model=HAIKU, pricing_version="v", result="COMPLETED"
    )
    assert "SHOP-7" in text and "Attempt          : 2" in text
    assert "claude-haiku-5-5, claude-sonnet-5-5" in text  # both models used
    assert "Attempt LLM Cost : $0.7000 USD" in text
    assert "Task LLM Cost    : $0.7100 USD over 2 attempt(s)" in text  # attempt 1 counted once
    assert "Correction: $0.2000" in text and "LLM API cost only" in text


async def test_failed_task_report_prints_its_accumulated_cost() -> None:
    _store, query, _, _, _, b1 = await _scenario()
    out: list[str] = []

    async def status(_: uuid.UUID) -> tuple[str, bool]:
        return "FAILED", True

    reporter = CostReporter(
        query, PriceTable(), provider="anthropic", primary_model=HAIKU,
        session_id=uuid.uuid4(), status_of=status, write=out.append,
    )  # fmt: skip
    await reporter.attempt_report(b1)
    assert "Workflow Result  : FAILED" in out[0] and "$0.0500" in out[0]


async def test_worker_restart_recovers_session_and_task_totals_from_persisted_records() -> None:
    store, query, _s1, s2, _, _ = await _scenario()
    # a fresh reporter (new process) only has the persisted records
    out: list[str] = []

    async def status(_: uuid.UUID) -> tuple[str, bool]:
        return "COMPLETED", True

    reporter = CostReporter(
        MemoryQuery(store, query.attempts), PriceTable(), provider="anthropic",
        primary_model=HAIKU, session_id=s2, status_of=status, write=out.append,
    )  # fmt: skip
    await reporter.print_session_summary()
    text = out[0]
    assert "Completed Tasks  : 1" in text and "Failed Tasks     : 1" in text
    assert "Total Attempts   : 2" in text
    assert "Session LLM Cost : $0.7500 USD" in text  # 0.50 + 0.20 + 0.05: session 2 only
    shop7 = next(line for line in text.splitlines() if line.startswith("SHOP-7"))
    assert "$0.7000" in shop7 and "$0.7100" in shop7  # session vs. cumulative task cost


async def test_session_summary_flags_missing_usage() -> None:
    store, query, _, s2, a2, _ = await _scenario()
    await handler(store, a2, "implementation", s2).on_llm_error(RuntimeError(), run_id=uuid.uuid4())
    rows = await query.rows(session_id=s2)
    text = session_summary(
        s2, rows, await query.rows(issue_keys=["SHOP-7", "SHOP-8"]), pricing_version="v"
    )
    assert "INCOMPLETE" in text and "unknown, not zero" in text


async def test_paused_attempts_are_not_reported_until_they_finish() -> None:
    _store, query, _, _, a2, _ = await _scenario()
    out: list[str] = []

    async def status(_: uuid.UUID) -> tuple[str, bool]:
        return "AWAITING_PLAN_APPROVAL", False

    reporter = CostReporter(
        query, PriceTable(), provider="anthropic", primary_model=HAIKU,
        session_id=uuid.uuid4(), status_of=status, write=out.append,
    )  # fmt: skip

    async def launch(_: uuid.UUID) -> str:
        return "paused"

    assert await reporter.around(launch)(a2) == "paused"
    assert out == []


def test_bedrock_usage_and_ids_are_understood() -> None:
    from ai_code_change_agent.application.costs.pricing import bedrock_base_model

    # langchain-aws: usage_metadata input_tokens = uncached + cache read + cache write
    msg = AIMessage(
        content="x",
        response_metadata={"model_name": "us.anthropic.claude-sonnet-5-5-v1:0"},
        usage_metadata={
            "input_tokens": 1500,
            "output_tokens": 100,
            "total_tokens": 1600,
            "input_token_details": {"cache_read": 1000, "cache_creation": 300},
        },
    )
    reported = extract_usage(msg)
    assert reported.usage == TokenUsage(200, 100, 300, 0, 1000)
    assert reported.model == "us.anthropic.claude-sonnet-5-5-v1:0"
    assert bedrock_base_model("us.anthropic.claude-sonnet-5-5-v1:0") == "claude-sonnet-5-5"
    assert bedrock_base_model("anthropic.claude-opus-5-5") == "claude-opus-5-5"
    assert bedrock_base_model("claude-haiku-5-5") == "claude-haiku-5-5"
    table = PriceTable()
    assert table.price(reported.model, reported.usage) is None  # reports: unknown, not guessed
    assert table.estimate(reported.model, reported.usage) is not None  # guards: list price


async def test_searches_are_charged_to_the_phase_cost_guard_and_stop_further_calls() -> None:
    from decimal import Decimal
    from types import SimpleNamespace

    from ai_code_change_agent.application.costs.usage import BudgetExceededError, BudgetGuard

    a = uuid.uuid4()
    thread = f"attempt:{a}:planning:x"
    store = MemoryStore()
    tracker = UsageTracker(
        store, PriceTable(), provider="anthropic", model=HAIKU, phase_budget_usd=Decimal("0.05")
    )
    guard = next(h for h in tracker.handlers(thread) if isinstance(h, BudgetGuard))
    assert tracker.search_allowed(thread)
    search = SimpleNamespace(
        model="unpriced-model", input_tokens=10_000, output_tokens=100, searches=5, error=None
    )
    await tracker.record_search(thread, search)  # unpriced: conservative rate + $0.05 fees
    assert guard.spent >= Decimal("0.05") and not tracker.search_allowed(thread)
    with pytest.raises(BudgetExceededError):
        await guard.on_chat_model_start({}, [])
    (record,) = store.records
    assert record.stage == "research" and record.cost_usd is None  # ledger: no invented price
    failed = SimpleNamespace(
        model=HAIKU, input_tokens=100, output_tokens=1, searches=1, error="search_x"
    )
    await tracker.record_search(thread, failed)
    assert store.records[-1].succeeded is False and store.records[-1].cost_usd is not None


# ---- provider failures, duplicates and unpriced models in the ledger -----------------------


def _error_response(status: int) -> LLMResult:
    """What LangChain passes to on_llm_error for a failed non-streaming call: the provider's
    error metadata only (see langchain_core _generate_response_from_error)."""
    meta = {"body": {"type": "error"}, "headers": {}, "status_code": status}
    return LLMResult(generations=[[ChatGeneration(message=AIMessage("", response_metadata=meta))]])


async def test_a_failed_call_without_streamed_usage_is_unknown_and_adds_nothing_known() -> None:
    from ai_code_change_agent.application.costs.usage import BudgetGuard

    store, session, attempt = MemoryStore(), uuid.uuid4(), uuid.uuid4()
    guard = BudgetGuard(PriceTable(), D("1"))
    await guard.on_llm_end(result(anthropic_message(1000, 1000, model=SONNET)))
    spent = guard.spent
    err = RuntimeError("overloaded")
    await guard.on_llm_error(err, run_id=uuid.uuid4(), response=_error_response(529))
    await handler(store, attempt, "implementation", session).on_llm_error(
        err, run_id=uuid.uuid4(), response=_error_response(529)
    )
    (rec,) = store.records
    assert not rec.succeeded and rec.usage is None and rec.cost_usd is None  # unknown, not zero
    assert guard.spent == spent == D("0.012")  # the earlier spend is kept


class AcceptingStore:
    """Records every add and accepts it: no dedupe, so the test sees what the tracker sends."""

    def __init__(self) -> None:
        self.added: list[LlmCallRecord] = []

    async def add(self, record: LlmCallRecord) -> bool:
        self.added.append(record)
        return True


async def test_a_replayed_accounting_event_is_handed_to_the_ledger_which_owns_the_dedupe() -> None:
    """The tracker does no cross-handler dedupe: a resumed phase run builds new handlers, so a
    replayed provider message reaches the store again under a new call id with its provider
    message id intact. The PostgreSQL ledger refuses it on uq_llm_usage_records_provider_message_id
    (tests/integration/test_usage_ledger_db.py::test_the_ledger_is_idempotent_append_only_and_
    survives_restarts); this pins the tracker side of that contract."""
    store, a = AcceptingStore(), uuid.uuid4()
    tracker = UsageTracker(
        store, PriceTable(), provider="anthropic", model=HAIKU, phase_budget_usd=D("1")
    )
    thread = f"attempt:{a}:planning:run1"
    first = next(h for h in tracker.handlers(thread) if isinstance(h, UsageCallbackHandler))
    again = next(  # the resumed run
        h for h in tracker.handlers(thread) if isinstance(h, UsageCallbackHandler)
    )
    assert again is not first
    await first.on_llm_end(
        result(anthropic_message(100_000, 0, msg_id="msg_dup")), run_id=uuid.uuid4()
    )
    await again.on_llm_end(
        result(anthropic_message(999_999, 999_999, msg_id="msg_dup")), run_id=uuid.uuid4()
    )
    original, replayed = store.added
    assert original.provider_message_id == replayed.provider_message_id == "msg_dup"
    assert original.call_id != replayed.call_id  # only the provider message id can dedupe it
    assert original.attempt_id == replayed.attempt_id == a
    assert original.usage == TokenUsage(100_000, 0) and original.cost_usd == D("0.01")


async def test_an_error_after_usage_was_recorded_keeps_the_recorded_usage() -> None:
    store, session, attempt = MemoryStore(), uuid.uuid4(), uuid.uuid4()
    h = handler(store, attempt, "implementation", session)
    run = uuid.uuid4()
    await h.on_llm_end(result(anthropic_message(100_000, 0, msg_id="msg_ok")), run_id=run)
    (recorded,) = store.records
    # the same run reporting an error afterwards does not replace or add to the recorded call
    await h.on_llm_error(RuntimeError("late failure"), run_id=run)
    assert store.records == [recorded] and recorded.succeeded
    # a later call that fails is added next to it; the known cost stays
    await h.on_llm_error(RuntimeError("overloaded"), run_id=uuid.uuid4())
    kept, failed = store.records
    assert kept is recorded and kept.usage == TokenUsage(100_000, 0)
    assert not failed.succeeded and failed.usage is None
    t = totals(store.records)
    assert (t.calls, t.known_calls, t.cost) == (2, 1, D("0.01"))
    assert t.money().startswith("at least $0.0100") and "INCOMPLETE: 1" in t.money()


async def test_an_unpriced_model_is_recorded_as_unknown_cost_and_flagged_in_reports(
    tmp_path: Path,
) -> None:
    store, session, attempt = MemoryStore(), uuid.uuid4(), uuid.uuid4()
    query = MemoryQuery(store, {attempt: ("SHOP-9", 1, "COMPLETED")})
    h = handler(store, attempt, "planning", session)
    await h.on_llm_end(result(anthropic_message(100_000, 0)), run_id=uuid.uuid4())  # $0.01
    await h.on_llm_end(
        result(anthropic_message(5_000, 500, model="claude-future-9")), run_id=uuid.uuid4()
    )
    priced_rec, unpriced = store.records
    assert unpriced.model == "claude-future-9" and unpriced.succeeded
    assert unpriced.usage == TokenUsage(5_000, 500)  # the tokens are kept...
    assert unpriced.cost_usd is None  # ...but the cost is unknown, never 0
    rows = await query.rows(session_id=session)
    summary = session_summary(session, rows, rows, pricing_version="v")
    shop9 = next(line for line in summary.splitlines() if line.startswith("SHOP-9"))
    assert shop9.endswith(" *")
    assert "Session LLM Cost : at least $0.0100" in summary and "unknown, not zero" in summary
    report = task_report(
        attempt, rows, provider="anthropic", primary_model=HAIKU, pricing_version="v"
    )
    assert "INCOMPLETE: 1 call(s)" in report
    # a price file that does not list the model: still unknown, not zero
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"version": "x", "models": {}}))
    assert PriceTable.from_file(path).price("claude-future-9", TokenUsage(1, 1)) is None
    assert priced_rec.cost_usd == D("0.01")
