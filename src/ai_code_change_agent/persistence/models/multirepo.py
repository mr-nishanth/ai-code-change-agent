"""Multi-repository executions: one parent per Story, one child per declared repository."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_code_change_agent.persistence.models.base import Base

PARENT_STATES = ("RUNNING", "COMPLETED", "PARTIALLY_COMPLETED", "FAILED", "BLOCKED")
REPORT_STATES = (
    "PENDING", "IN_PROGRESS", "SUCCEEDED", "RETRYABLE_FAILURE", "PERMANENT_FAILURE", "DISABLED",
)  # fmt: skip
CHILD_STATES = ("WAITING", "QUEUED", "RUNNING", "COMPLETED", "FAILED", "REJECTED", "BLOCKED")


def _in(values: tuple[str, ...]) -> str:
    return f"status IN ({', '.join(repr(v) for v in values)})"


class CodeChangeParentModel(Base):
    __tablename__ = "code_change_parents"
    __table_args__ = (
        CheckConstraint(_in(PARENT_STATES), name="status_valid"),
        CheckConstraint(
            "jira_report IS NULL OR jira_report IN ("
            + ", ".join(repr(s) for s in REPORT_STATES)
            + ")",
            name="jira_report_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("code_change_requests.id", ondelete="RESTRICT"), unique=True
    )
    status: Mapped[str] = mapped_column(String(32))
    declaration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    report_round: Mapped[int] = mapped_column(Integer, server_default="0")
    jira_report: Mapped[str | None] = mapped_column(String(24))  # ReportState; NULL = PENDING
    jira_comment_id: Mapped[str | None] = mapped_column(String(32))
    jira_report_attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    jira_report_error: Mapped[str | None] = mapped_column(String(120))
    jira_report_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CodeChangeChildModel(Base):
    __tablename__ = "code_change_children"
    __table_args__ = (
        CheckConstraint(_in(CHILD_STATES), name="status_valid"),
        CheckConstraint(
            "contract_sha256 IS NULL OR contract_sha256 ~ '^[0-9a-f]{64}$'",
            name="contract_sha256_hex",
        ),
        UniqueConstraint("parent_id", "repository_id", name="uq_code_change_children_parent_id"),
        UniqueConstraint("parent_id", "position", name="uq_code_change_children_position"),
        Index("ix_code_change_children_attempt_id", "attempt_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    parent_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("code_change_parents.id", ondelete="RESTRICT")
    )
    position: Mapped[int] = mapped_column(Integer)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repositories.id", ondelete="RESTRICT")
    )
    profile_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    repository_key: Mapped[str] = mapped_column(String(63))
    reference: Mapped[str] = mapped_column(String(200))
    role: Mapped[str | None] = mapped_column(String(40))
    responsibility: Mapped[str | None] = mapped_column(Text)
    depends_on: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    runs: Mapped[int] = mapped_column(Integer, server_default="0")
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("code_change_attempts.id", ondelete="RESTRICT")
    )
    contract_ref: Mapped[str | None] = mapped_column(String(64))
    contract_sha256: Mapped[str | None] = mapped_column(String(64))
    pr_url: Mapped[str | None] = mapped_column(String(300))
    branch: Mapped[str | None] = mapped_column(String(120))
    failure_code: Mapped[str | None] = mapped_column(String(120))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
