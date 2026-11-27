"""PostgreSQL job queue: SELECT ... FOR UPDATE SKIP LOCKED claiming, owner-checked leases."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.jobs.queue import Job, JobStatus
from ai_code_change_agent.persistence.models.jobs import JobModel

_LEASE = "now() + make_interval(secs => CAST(:lease AS double precision))"
_RETRY = "CAST(:retry AS integer) IS NOT NULL"
_REAP = text(
    "UPDATE jobs SET status = 'DEAD', last_error = 'lease_expired_attempts_exhausted', "
    "lease_owner = NULL, lease_expires_at = NULL, finished_at = now(), updated_at = now() "
    "WHERE status = 'RUNNING' AND lease_expires_at < now() AND attempts >= max_attempts"
)
_CLAIM = text(
    f"UPDATE jobs SET status = 'RUNNING', lease_owner = :owner, lease_expires_at = {_LEASE}, "  # noqa: S608 - constant SQL fragments; values are bound
    "heartbeat_at = now(), attempts = attempts + 1, updated_at = now() "
    "WHERE id = (SELECT id FROM jobs WHERE attempts < max_attempts AND ("
    "  (status = 'QUEUED' AND available_at <= now()) OR "
    "  (status = 'RUNNING' AND lease_expires_at < now())) "
    "  ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
    "RETURNING id, kind, payload, attempts, max_attempts"
)
_BEAT = text(
    f"UPDATE jobs SET lease_expires_at = {_LEASE}, heartbeat_at = now(), updated_at = now() "  # noqa: S608 - constant SQL fragments; values are bound
    "WHERE id = :id AND status = 'RUNNING' AND lease_owner = :owner "
    "AND lease_expires_at >= now()"
)
_DONE = text(
    "UPDATE jobs SET status = 'SUCCEEDED', lease_owner = NULL, lease_expires_at = NULL, "
    "finished_at = now(), updated_at = now(), last_error = NULL "
    "WHERE id = :id AND status = 'RUNNING' AND lease_owner = :owner"
)
_FAIL = text(
    "UPDATE jobs SET "  # noqa: S608 - constant SQL fragments; values are bound
    f"status = CASE WHEN {_RETRY} AND attempts < max_attempts THEN 'QUEUED' ELSE 'DEAD' END, "
    f"available_at = CASE WHEN {_RETRY} "
    "  THEN now() + make_interval(secs => CAST(:retry AS double precision)) "
    "  ELSE available_at END, "
    f"finished_at = CASE WHEN {_RETRY} AND attempts < max_attempts THEN NULL ELSE now() END, "
    "lease_owner = NULL, lease_expires_at = NULL, last_error = :code, updated_at = now() "
    "WHERE id = :id AND status = 'RUNNING' AND lease_owner = :owner RETURNING status"
)


class PostgresJobQueue:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str, max_attempts: int
    ) -> bool:
        async with self._sessions() as session, session.begin():
            created = (
                await session.execute(
                    pg_insert(JobModel)
                    .values(
                        id=uuid.uuid4(),
                        kind=kind,
                        payload=payload,
                        dedupe_key=dedupe_key,
                        max_attempts=max_attempts,
                    )
                    .on_conflict_do_nothing(index_elements=["dedupe_key"])
                    .returning(JobModel.id)
                )
            ).scalar_one_or_none()
        return created is not None

    async def claim(self, owner: str, lease_seconds: int) -> Job | None:
        async with self._sessions() as session, session.begin():
            await session.execute(_REAP)
            row = (await session.execute(_CLAIM, {"owner": owner, "lease": lease_seconds})).first()
        if row is None:
            return None
        return Job(row.id, row.kind, dict(row.payload), row.attempts, row.max_attempts)

    async def heartbeat(self, job_id: uuid.UUID, owner: str, lease_seconds: int) -> bool:
        async with self._sessions() as session, session.begin():
            res = await session.execute(
                _BEAT, {"id": job_id, "owner": owner, "lease": lease_seconds}
            )
        return bool(getattr(res, "rowcount", 0))

    async def complete(self, job_id: uuid.UUID, owner: str) -> bool:
        async with self._sessions() as session, session.begin():
            res = await session.execute(_DONE, {"id": job_id, "owner": owner})
        return bool(getattr(res, "rowcount", 0))

    async def fail(
        self, job_id: uuid.UUID, owner: str, code: str, *, retry_in_seconds: int | None
    ) -> JobStatus:
        async with self._sessions() as session, session.begin():
            row = (
                await session.execute(
                    _FAIL,
                    {
                        "id": job_id,
                        "owner": owner,
                        "code": code[:200],
                        "retry": retry_in_seconds,
                    },
                )
            ).first()
        return JobStatus(row.status) if row else JobStatus.RUNNING  # RUNNING: lease was lost

    async def dispatch_pending(self) -> int:
        return 0  # claims read the table directly: there is no outbox to relay

    async def sweep(self) -> int:
        return 0  # no separate delivery channel, so nothing can be lost
