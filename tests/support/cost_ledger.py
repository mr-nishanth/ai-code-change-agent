"""A fixed LLM usage ledger and the cost reports built from it (Task 8 golden test).

The same fixture fed the PostgreSQL usage store once, to produce
`tests/integration/floci/golden/usage_reports.txt`; the DynamoDB store must reproduce that text
exactly. `render` asks the cost query the same questions `CostReporter` asks (the attempt, then the
whole Story; the session, then every Story it touched), plus a few edge filters, and prints the
rows in full so ordering, joins and decimal text are compared, not just the rounded totals.

Two sessions (an older one and the current one), three Stories: SHOP-1 with a failed and a
completed attempt, SHOP-2 across two repositories (per-repository totals), OPS-7 seen only by the
older session. Calls include a failed call (usage unknown), an unpriced model (usage known, cost
unknown), a call with no attempt, two calls with the same timestamp (ordered by call id) and costs
with more than ten decimal places (stored rounded half-up to ten, as `Numeric(16, 10)` did)."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ai_code_change_agent.application.costs.pricing import TokenUsage
from ai_code_change_agent.application.costs.report import (
    CostQuery,
    CostRow,
    session_summary,
    task_report,
)
from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.domain.changes.states import AttemptStatus

T0 = datetime(2026, 11, 20, 10, 0, 0, 123456, tzinfo=UTC)
PRICING = "anthropic-2026-11"
PROVIDER = "anthropic"
PRIMARY_MODEL = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


SESSION_OLD = _id(0x5E5_0001)
SESSION_NOW = _id(0x5E5_0002)


@dataclass(frozen=True)
class RepoSpec:
    key: str
    sql_id: uuid.UUID  # the id the SQL seed used; the DynamoDB registry allocates its own


@dataclass(frozen=True)
class RequestSpec:
    id: uuid.UUID
    jira_issue_id: str
    issue_key: str
    project_key: str


@dataclass(frozen=True)
class AttemptSpec:
    id: uuid.UUID
    request: RequestSpec
    number: int
    repository: str  # repository key
    status: AttemptStatus
    failure_code: str | None = None


REPOS = (RepoSpec("shop-api", _id(0xA1)), RepoSpec("shop-web", _id(0xA2)))
SHOP1 = RequestSpec(_id(0xB1), "910001", "SHOP-1", "SHOP")
SHOP2 = RequestSpec(_id(0xB2), "910002", "SHOP-2", "SHOP")
OPS7 = RequestSpec(_id(0xB3), "910003", "OPS-7", "OPS")
REQUESTS = (SHOP1, SHOP2, OPS7)

A11 = AttemptSpec(_id(0xC11), SHOP1, 1, "shop-api", AttemptStatus.FAILED, "validation_failed")
A12 = AttemptSpec(_id(0xC12), SHOP1, 2, "shop-api", AttemptStatus.COMPLETED)
A21 = AttemptSpec(_id(0xC21), SHOP2, 1, "shop-api", AttemptStatus.COMPLETED)
A22 = AttemptSpec(_id(0xC22), SHOP2, 2, "shop-web", AttemptStatus.FAILED, "tests_failed")
A31 = AttemptSpec(_id(0xC31), OPS7, 1, "shop-api", AttemptStatus.REJECTED)
ATTEMPTS = (A11, A12, A21, A22, A31)
UNKNOWN_ATTEMPT = _id(0xCFF)  # never created: its report has no rows


def _rec(
    n: int,
    seconds: int,
    session: uuid.UUID,
    attempt: AttemptSpec | None,
    stage: str,
    model: str,
    usage: TokenUsage | None,
    cost: str | None,
    **extra: object,
) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=_id(0xD000 + n),
        session_id=session,
        attempt_id=attempt.id if attempt else None,
        stage=stage,
        provider=PROVIDER,
        model=model,
        succeeded=extra.pop("succeeded", True),  # type: ignore[arg-type]
        usage=usage,
        cost_usd=Decimal(cost) if cost is not None else None,
        pricing_version=PRICING,
        created_at=T0 + timedelta(seconds=seconds),
        **extra,  # type: ignore[arg-type]
    )


U = TokenUsage
RECORDS: tuple[LlmCallRecord, ...] = (
    # older session
    _rec(
        1,
        0,
        SESSION_OLD,
        A11,
        "planning",
        HAIKU,
        U(1200, 300, 100, 0, 50),
        "0.0123456789",
        provider_message_id="msg_a11_1",
    ),
    _rec(
        2,
        10,
        SESSION_OLD,
        A11,
        "implementation",
        SONNET,
        U(5400, 900, 0, 2000, 800),
        "0.25",
        provider_message_id="msg_a11_2",
        inference_geo="us",
    ),
    _rec(
        3,
        20,
        SESSION_OLD,
        A11,
        "implementation",
        SONNET,
        None,
        None,
        succeeded=False,
        error_code="APITimeoutError",
    ),
    _rec(
        4,
        30,
        SESSION_OLD,
        A31,
        "planning",
        HAIKU,
        U(800, 120),
        "0.001",
        provider_message_id="msg_a31_1",
    ),
    # current session
    _rec(
        5,
        100,
        SESSION_NOW,
        A12,
        "planning",
        HAIKU,
        U(1500, 400, 0, 0, 1000),
        "0.00000000015",
        provider_message_id="msg_a12_1",
    ),
    _rec(
        7,
        110,
        SESSION_NOW,
        A12,
        "implementation",
        SONNET,
        U(250000, 4000, 300, 0, 0),
        "1.23456789012345",
        provider_message_id="msg_a12_3",
        long_context=True,
    ),
    _rec(
        6,
        110,
        SESSION_NOW,
        A12,
        "implementation",
        SONNET,
        U(9000, 1100),
        "0.0441",
        provider_message_id="msg_a12_2",
    ),
    _rec(
        8,
        120,
        SESSION_NOW,
        A12,
        "correction",
        SONNET,
        U(7000, 650, 0, 0, 6000),
        "0.03015",
        provider_message_id="msg_a12_4",
    ),
    _rec(9, 130, SESSION_NOW, A12, "research", HAIKU, U(3000, 200), "0.0130000000"),
    _rec(
        10,
        140,
        SESSION_NOW,
        A21,
        "planning",
        HAIKU,
        U(1000, 100),
        "0.0015",
        provider_message_id="msg_a21_1",
    ),
    _rec(
        11,
        150,
        SESSION_NOW,
        A21,
        "implementation",
        "mystery-model-1",
        U(2000, 500),
        None,
        provider_message_id="msg_a21_2",
    ),
    _rec(
        12,
        160,
        SESSION_NOW,
        A22,
        "planning",
        HAIKU,
        U(1100, 90),
        "0.0016",
        provider_message_id="msg_a22_1",
    ),
    _rec(
        13,
        170,
        SESSION_NOW,
        A22,
        "implementation",
        SONNET,
        U(6000, 700),
        "0.0285",
        provider_message_id="msg_a22_2",
    ),
    _rec(14, 180, SESSION_NOW, None, "research", HAIKU, U(500, 50), "0.0105"),
)


def _dump(title: str, rows: Sequence[CostRow]) -> str:
    lines = [f"## rows: {title} ({len(rows)})"]
    for row in rows:
        r = row.record
        u = r.usage
        tokens = (
            "-"
            if u is None
            else f"{u.input_tokens}/{u.output_tokens}/{u.cache_write_5m_tokens}/"
            f"{u.cache_write_1h_tokens}/{u.cache_read_tokens}"
        )
        lines.append(
            " | ".join(
                str(v)
                for v in (
                    r.created_at.astimezone(UTC).isoformat(),
                    r.call_id,
                    r.session_id,
                    r.attempt_id,
                    row.issue_key,
                    row.attempt_number,
                    row.attempt_status,
                    row.repository,
                    r.stage,
                    r.provider,
                    r.model,
                    r.provider_message_id,
                    r.succeeded,
                    tokens,
                    r.cost_usd,
                    r.long_context,
                    r.inference_geo,
                    r.pricing_version,
                    r.error_code,
                )
            )
        )
    return "\n".join(lines)


async def _task_report(query: CostQuery, attempt_id: uuid.UUID, result: str | None) -> str:
    """As `CostReporter.attempt_report` builds it."""
    head = await query.rows(attempt_ids=[attempt_id])
    issue = next((r.issue_key for r in head if r.issue_key), None)
    rows = await query.rows(issue_keys=[issue]) if issue else head
    report = task_report(
        attempt_id,
        rows,
        provider=PROVIDER,
        primary_model=PRIMARY_MODEL,
        pricing_version=PRICING,
        result=result,
    )
    return f"## task_report {attempt_id}\n{report}"


async def _session_summary(query: CostQuery, session_id: uuid.UUID) -> str:
    """As `CostReporter.print_session_summary` builds it."""
    mine = await query.rows(session_id=session_id)
    issues = sorted({r.issue_key for r in mine if r.issue_key})
    tasks = await query.rows(issue_keys=issues) if issues else []
    summary = session_summary(session_id, mine, tasks, pricing_version=PRICING)
    return f"## session_summary {session_id}\n{summary}"


async def render(query: CostQuery) -> str:
    sections = [
        _dump("attempt A12", await query.rows(attempt_ids=[A12.id])),
        _dump("attempts A11+A22", await query.rows(attempt_ids=[A22.id, A11.id])),
        _dump("issue SHOP-1", await query.rows(issue_keys=["SHOP-1"])),
        _dump("issues SHOP-2+OPS-7", await query.rows(issue_keys=["SHOP-2", "OPS-7"])),
        _dump("issue NOPE-1", await query.rows(issue_keys=["NOPE-1"])),
        _dump("session now", await query.rows(session_id=SESSION_NOW)),
        _dump("session old", await query.rows(session_id=SESSION_OLD)),
        _dump(
            "issue SHOP-1 in session now",
            await query.rows(issue_keys=["SHOP-1"], session_id=SESSION_NOW),
        ),
        _dump(
            "attempts A11+A12 in session old",
            await query.rows(attempt_ids=[A11.id, A12.id], session_id=SESSION_OLD),
        ),
        _dump(
            "attempt A21 and issue SHOP-1",
            await query.rows(attempt_ids=[A21.id], issue_keys=["SHOP-1"]),
        ),
        _dump("no attempts", await query.rows(attempt_ids=[])),
        _dump("no issues", await query.rows(issue_keys=[])),
        await _task_report(query, A12.id, "COMPLETED"),
        await _task_report(query, A11.id, None),
        await _task_report(query, A22.id, "FAILED"),
        await _task_report(query, UNKNOWN_ATTEMPT, None),
        await _session_summary(query, SESSION_NOW),
        await _session_summary(query, SESSION_OLD),
    ]
    return "\n\n".join(sections) + "\n"
