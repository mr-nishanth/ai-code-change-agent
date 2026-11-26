"""PostgreSQL adapter. Delivery + code change request + intake job are written in ONE transaction.

Idempotency is enforced by unique indexes (delivery_id, jira_issue_id, job dedupe_key) with
INSERT ... ON CONFLICT DO NOTHING, so concurrent duplicates are safe without in-memory state. A
duplicate delivery or a second delivery for the same Story never enqueues another job.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.domain.webhooks.models import (
    IssueRef,
    Outcome,
    PersistenceError,
    ProcessingStatus,
    ReceivedDelivery,
)
from ai_code_change_agent.persistence.models.jobs import JobModel
from ai_code_change_agent.persistence.models.webhooks import CodeChangeRequest, JiraDelivery

INTAKE_JOB_KIND = "code_change.intake"


class SqlAlchemyWebhookRepository:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, job_max_attempts: int = 3
    ) -> None:
        self._session_factory = session_factory
        self._max_attempts = job_max_attempts

    async def record(self, delivery: ReceivedDelivery) -> Outcome:
        try:
            async with self._session_factory() as session, session.begin():
                return await self._record(session, delivery)
        except Exception as exc:
            # Transaction is rolled back by the context manager. Expose the class name only:
            # driver messages can contain the DSN or query parameters.
            raise PersistenceError(type(exc).__name__) from exc

    async def _record(self, session: AsyncSession, delivery: ReceivedDelivery) -> Outcome:
        issue = delivery.issue
        ignored = delivery.status is ProcessingStatus.IGNORED
        inserted = (
            await session.execute(
                pg_insert(JiraDelivery)
                .values(
                    id=uuid.uuid4(),
                    delivery_id=delivery.delivery_id,
                    payload_sha256=delivery.payload_sha256,
                    event_type=delivery.event_type,
                    jira_issue_id=issue.issue_id if issue else None,
                    jira_issue_key=issue.issue_key if issue else None,
                    jira_project_key=issue.project_key if issue else None,
                    jira_issue_type=issue.issue_type if issue else None,
                    retry_count=delivery.retry_count,
                    flow=delivery.flow,
                    verification_result="VERIFIED",
                    processing_result="IGNORED" if ignored else "ACCEPTED",
                    ignore_reason=delivery.ignore_reason.value if delivery.ignore_reason else None,
                )
                .on_conflict_do_nothing(index_elements=["delivery_id"])
                .returning(JiraDelivery.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return Outcome.DUPLICATE  # same delivery id already recorded
        if ignored or issue is None:
            return Outcome.IGNORED

        code_change_request_id = uuid.uuid4()
        created = (
            await session.execute(
                pg_insert(CodeChangeRequest)
                .values(
                    id=code_change_request_id,
                    jira_issue_id=issue.issue_id,
                    jira_issue_key=issue.issue_key,
                    jira_project_key=issue.project_key,
                )
                .on_conflict_do_nothing(index_elements=["jira_issue_id"])
                .returning(CodeChangeRequest.id)
            )
        ).scalar_one_or_none()
        if created is None:  # a different delivery already created this Story's code change request
            existing = (
                await session.execute(
                    select(CodeChangeRequest.id).where(
                        CodeChangeRequest.jira_issue_id == issue.issue_id
                    )
                )
            ).scalar_one()
            await session.execute(
                update(JiraDelivery)
                .where(JiraDelivery.id == inserted)
                .values(processing_result="DUPLICATE_ISSUE", code_change_request_id=existing)
            )
            return Outcome.DUPLICATE

        await self._enqueue_intake(session, code_change_request_id, issue)
        await session.execute(
            update(JiraDelivery)
            .where(JiraDelivery.id == inserted)
            .values(code_change_request_id=code_change_request_id)
        )
        return Outcome.ACCEPTED

    async def _enqueue_intake(
        self, session: AsyncSession, code_change_request_id: uuid.UUID, issue: IssueRef
    ) -> None:
        await session.execute(
            pg_insert(JobModel)
            .values(
                id=uuid.uuid4(),
                kind=INTAKE_JOB_KIND,
                payload={
                    "code_change_request_id": str(code_change_request_id),
                    "jira_issue_id": issue.issue_id,
                    "jira_issue_key": issue.issue_key,
                },
                dedupe_key=f"{INTAKE_JOB_KIND}:{code_change_request_id}",
                max_attempts=self._max_attempts,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
