"""PostgreSQL store for multi-repository parents and children (compare-and-set updates)."""

import uuid
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.multirepo.models import (
    Child,
    ChildStatus,
    Parent,
    ParentStatus,
)
from ai_code_change_agent.persistence.models.multirepo import (
    CodeChangeChildModel,
    CodeChangeParentModel,
)

_CHILD_FIELDS = {
    "status", "runs", "attempt_id", "contract_ref", "contract_sha256", "pr_url", "branch",
    "failure_code",
}  # fmt: skip


def _parent(m: CodeChangeParentModel) -> Parent:
    return Parent(
        m.id,
        m.request_id,
        ParentStatus(m.status),
        dict(m.declaration),
        m.report_round,
        m.jira_report,
    )


def _child(m: CodeChangeChildModel) -> Child:
    return Child(
        id=m.id,
        parent_id=m.parent_id,
        position=m.position,
        repository_id=m.repository_id,
        profile_version_id=m.profile_version_id,
        repository_key=m.repository_key,
        reference=m.reference,
        role=m.role,
        responsibility=m.responsibility,
        depends_on=tuple(m.depends_on),
        status=ChildStatus(m.status),
        runs=m.runs,
        attempt_id=m.attempt_id,
        contract_ref=m.contract_ref,
        contract_sha256=m.contract_sha256,
        pr_url=m.pr_url,
        branch=m.branch,
        failure_code=m.failure_code,
    )


class PostgresMultiRepoStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, parent: Parent, children: list[Child]) -> tuple[Parent, list[Child]]:
        async with self._sessions() as session, session.begin():
            inserted = (
                await session.execute(
                    pg_insert(CodeChangeParentModel)
                    .values(
                        id=parent.id,
                        request_id=parent.request_id,
                        status=parent.status.value,
                        declaration=parent.declaration,
                    )
                    .on_conflict_do_nothing(index_elements=["request_id"])
                    .returning(CodeChangeParentModel.id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                for c in children:
                    session.add(
                        CodeChangeChildModel(
                            id=c.id,
                            parent_id=parent.id,
                            position=c.position,
                            repository_id=c.repository_id,
                            profile_version_id=c.profile_version_id,
                            repository_key=c.repository_key,
                            reference=c.reference,
                            role=c.role,
                            responsibility=c.responsibility,
                            depends_on=list(c.depends_on),
                            status=c.status.value,
                        )
                    )
        found = await self.find_by_request(parent.request_id)
        assert found is not None  # noqa: S101
        return found

    async def find_by_request(self, request_id: uuid.UUID) -> tuple[Parent, list[Child]] | None:
        async with self._sessions() as session:
            m = (
                await session.execute(
                    select(CodeChangeParentModel).where(
                        CodeChangeParentModel.request_id == request_id
                    )
                )
            ).scalar_one_or_none()
            if m is None:
                return None
            return _parent(m), await self._children(session, m.id)

    async def load(self, parent_id: uuid.UUID) -> tuple[Parent, list[Child]]:
        async with self._sessions() as session:
            m = await session.get(CodeChangeParentModel, parent_id)
            if m is None:
                raise LookupError("parent_not_found")
            return _parent(m), await self._children(session, parent_id)

    @staticmethod
    async def _children(session: AsyncSession, parent_id: uuid.UUID) -> list[Child]:
        rows = (
            await session.execute(
                select(CodeChangeChildModel)
                .where(CodeChangeChildModel.parent_id == parent_id)
                .order_by(CodeChangeChildModel.position)
            )
        ).scalars()
        return [_child(m) for m in rows]

    async def child(self, child_id: uuid.UUID) -> Child | None:
        async with self._sessions() as session:
            m = await session.get(CodeChangeChildModel, child_id)
            return _child(m) if m else None

    async def child_by_attempt(self, attempt_id: uuid.UUID) -> Child | None:
        async with self._sessions() as session:
            m = (
                await session.execute(
                    select(CodeChangeChildModel).where(
                        CodeChangeChildModel.attempt_id == attempt_id
                    )
                )
            ).scalar_one_or_none()
            return _child(m) if m else None

    async def update_child(
        self, child_id: uuid.UUID, expected: frozenset[ChildStatus], **fields: Any
    ) -> bool:
        unknown = set(fields) - _CHILD_FIELDS
        if unknown:
            raise ValueError(f"unknown child fields: {sorted(unknown)}")
        values = {k: (v.value if isinstance(v, ChildStatus) else v) for k, v in fields.items()}
        async with self._sessions() as session, session.begin():
            done = (
                await session.execute(
                    update(CodeChangeChildModel)
                    .where(
                        CodeChangeChildModel.id == child_id,
                        CodeChangeChildModel.status.in_([s.value for s in expected]),
                    )
                    .values(**values, updated_at=text("now()"))
                    .returning(CodeChangeChildModel.id)
                )
            ).scalar_one_or_none()
        return done is not None

    async def update_parent(
        self, parent_id: uuid.UUID, expected: frozenset[ParentStatus], **fields: Any
    ) -> bool:
        values: dict[str, Any] = {"updated_at": text("now()")}
        if "status" in fields:
            values["status"] = ParentStatus(fields["status"]).value
        if fields.get("finished"):
            values["completed_at"] = text("now()")
        if fields.get("reopen"):
            values["completed_at"] = None
            values["jira_report"] = None
            values["report_round"] = CodeChangeParentModel.report_round + 1
        async with self._sessions() as session, session.begin():
            done = (
                await session.execute(
                    update(CodeChangeParentModel)
                    .where(
                        CodeChangeParentModel.id == parent_id,
                        CodeChangeParentModel.status.in_([s.value for s in expected]),
                    )
                    .values(**values)
                    .returning(CodeChangeParentModel.id)
                )
            ).scalar_one_or_none()
        return done is not None

    async def claim_report(self, parent_id: uuid.UUID) -> bool:
        async with self._sessions() as session, session.begin():
            done = (
                await session.execute(
                    update(CodeChangeParentModel)
                    .where(
                        CodeChangeParentModel.id == parent_id,
                        CodeChangeParentModel.jira_report.is_(None),
                    )
                    .values(jira_report="POSTING")
                    .returning(CodeChangeParentModel.id)
                )
            ).scalar_one_or_none()
        return done is not None

    async def finish_report(self, parent_id: uuid.UUID, posted: bool) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                update(CodeChangeParentModel)
                .where(CodeChangeParentModel.id == parent_id)
                .values(jira_report="POSTED" if posted else None)  # a failed post may be retried
            )

    async def set_request_status(self, request_id: uuid.UUID, status: str) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE code_change_requests SET status = :s, updated_at = now() WHERE id = :id"
                ),
                {"s": status, "id": request_id},
            )
