"""PostgreSQL usage ledger: idempotent inserts and the joined rows the cost reports read."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.costs.pricing import TokenUsage
from ai_code_change_agent.application.costs.report import CostRow
from ai_code_change_agent.application.costs.usage import LlmCallRecord
from ai_code_change_agent.persistence.models.usage import LlmUsageRecordModel

_ROWS = """
SELECT u.*, r.jira_issue_key, a.number AS attempt_number, a.status AS attempt_status,
       repo.repository_key
FROM llm_usage_records u
LEFT JOIN code_change_attempts a ON a.id = u.attempt_id
LEFT JOIN code_change_requests r ON r.id = a.request_id
LEFT JOIN repositories repo ON repo.id = a.repository_id
WHERE (CAST(:all_attempts AS boolean) OR u.attempt_id IN :attempt_ids)
  AND (CAST(:all_issues AS boolean) OR r.jira_issue_key IN :issue_keys)
  AND (CAST(:session_id AS uuid) IS NULL OR u.session_id = CAST(:session_id AS uuid))
ORDER BY u.created_at, u.id
"""


class PostgresUsageStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(self, record: LlmCallRecord) -> bool:
        u = record.usage
        values: dict[str, Any] = {
            "id": record.call_id,
            "session_id": record.session_id,
            "attempt_id": record.attempt_id,
            "stage": record.stage[:32],
            "provider": record.provider[:32],
            "model": record.model[:128],
            "provider_message_id": record.provider_message_id,
            "succeeded": record.succeeded,
            "input_tokens": u.input_tokens if u else None,
            "output_tokens": u.output_tokens if u else None,
            "cache_write_5m_tokens": u.cache_write_5m_tokens if u else None,
            "cache_write_1h_tokens": u.cache_write_1h_tokens if u else None,
            "cache_read_tokens": u.cache_read_tokens if u else None,
            "cost_usd": record.cost_usd if u else None,
            "long_context": record.long_context,
            "inference_geo": record.inference_geo,
            "pricing_version": record.pricing_version[:64],
            "error_code": record.error_code,
            "created_at": record.created_at,
        }
        stmt = (
            pg_insert(LlmUsageRecordModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(LlmUsageRecordModel.id)
        )
        async with self._sessions() as session, session.begin():
            inserted = (await session.execute(stmt)).scalar_one_or_none()
        return inserted is not None

    async def rows(
        self,
        *,
        attempt_ids: Sequence[uuid.UUID] | None = None,
        issue_keys: Sequence[str] | None = None,
        session_id: uuid.UUID | None = None,
    ) -> list[CostRow]:
        stmt = text(_ROWS).bindparams(
            bindparam("attempt_ids", expanding=True), bindparam("issue_keys", expanding=True)
        )
        params = {
            "all_attempts": attempt_ids is None,
            "attempt_ids": list(attempt_ids or [uuid.UUID(int=0)]),
            "all_issues": issue_keys is None,
            "issue_keys": list(issue_keys or [""]),
            "session_id": str(session_id) if session_id else None,
        }
        async with self._sessions() as session:
            result = await session.execute(stmt, params)
            return [_row(m) for m in result.mappings().all()]


def _row(m: Any) -> CostRow:
    usage = (
        TokenUsage(
            m["input_tokens"],
            m["output_tokens"],
            m["cache_write_5m_tokens"] or 0,
            m["cache_write_1h_tokens"] or 0,
            m["cache_read_tokens"] or 0,
        )
        if m["input_tokens"] is not None
        else None
    )
    record = LlmCallRecord(
        call_id=m["id"],
        session_id=m["session_id"],
        attempt_id=m["attempt_id"],
        stage=m["stage"],
        provider=m["provider"],
        model=m["model"],
        succeeded=m["succeeded"],
        usage=usage,
        cost_usd=m["cost_usd"],
        pricing_version=m["pricing_version"],
        provider_message_id=m["provider_message_id"],
        long_context=m["long_context"],
        inference_geo=m["inference_geo"],
        error_code=m["error_code"],
        created_at=m["created_at"],
    )
    return CostRow(
        record, m["jira_issue_key"], m["attempt_number"], m["attempt_status"], m["repository_key"]
    )
