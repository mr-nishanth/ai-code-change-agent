"""PostgreSQL usage ledger: idempotent inserts, append-only rows, joined report rows."""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.costs.pricing import TokenUsage
from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.persistence.repositories.usage_store import PostgresUsageStore
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from tests.integration.conftest import libpq

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(database_url)
    yield create_session_factory(engine)
    await engine.dispose()


def record(session: uuid.UUID, msg: str | None, usage: TokenUsage | None) -> LlmCallRecord:
    return LlmCallRecord(
        call_id=uuid.uuid4(),
        session_id=session,
        attempt_id=None,
        stage="planning",
        provider="anthropic",
        model="claude-haiku-5-5",
        succeeded=usage is not None,
        usage=usage,
        cost_usd=Decimal("0.0001234") if usage else None,
        pricing_version="v",
        provider_message_id=msg,
    )


async def test_the_ledger_is_idempotent_append_only_and_survives_restarts(
    sessions: async_sessionmaker[AsyncSession], database_url: str
) -> None:
    session = uuid.uuid4()
    store = PostgresUsageStore(sessions)
    first = record(session, "msg_1", TokenUsage(10, 5, 1, 2, 3))
    assert await store.add(first)
    assert not await store.add(first)  # same call id
    assert not await store.add(record(session, "msg_1", TokenUsage(10, 5)))  # same provider msg
    assert await store.add(record(session, None, None))  # failed call, usage unknown
    reread = await PostgresUsageStore(sessions).rows(session_id=session)  # "after a restart"
    assert len(reread) == 2
    known = next(r for r in reread if r.record.usage is not None)
    assert known.record.usage == TokenUsage(10, 5, 1, 2, 3)
    assert known.record.cost_usd == Decimal("0.0001234")
    assert next(r for r in reread if r.record.usage is None).record.cost_usd is None
    with (
        psycopg.connect(libpq(database_url), autocommit=True) as conn,
        pytest.raises(psycopg.errors.IntegrityConstraintViolation),
    ):
        conn.execute("UPDATE llm_usage_records SET cost_usd = 0")
