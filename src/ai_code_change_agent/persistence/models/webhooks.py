"""Webhook intake tables: jira_deliveries (one row per verified delivery) and code_change_requests
(one row per Jira Story). The payload itself is never stored."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.persistence.models.base import Base

REQUEST_STATUSES = ("RECEIVED", *(s.value for s in AttemptStatus), "PARTIALLY_COMPLETED")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class CodeChangeRequest(Base):
    __tablename__ = "code_change_requests"
    __table_args__ = (CheckConstraint(_in("status", REQUEST_STATUSES), name="status_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    jira_issue_id: Mapped[str] = mapped_column(String(20), unique=True)
    jira_issue_key: Mapped[str] = mapped_column(String(80))
    jira_project_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), server_default="RECEIVED")
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("repositories.id", ondelete="RESTRICT")
    )
    profile_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("repository_profile_versions.id", ondelete="RESTRICT")
    )
    current_attempt_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JiraDelivery(Base):
    __tablename__ = "jira_deliveries"
    __table_args__ = (
        CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="payload_sha256_hex"),
        CheckConstraint("retry_count BETWEEN 0 AND 999", name="retry_count_range"),
        CheckConstraint("flow IS NULL OR flow IN ('Primary', 'Secondary')", name="flow_valid"),
        CheckConstraint("verification_result = 'VERIFIED'", name="verification_result_valid"),
        CheckConstraint(
            _in("processing_result", ("ACCEPTED", "IGNORED", "DUPLICATE_ISSUE")),
            name="processing_result_valid",
        ),
        Index("ix_jira_deliveries_code_change_request_id", "code_change_request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    delivery_id: Mapped[str] = mapped_column(String(255), unique=True)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(100))
    jira_issue_id: Mapped[str | None] = mapped_column(String(20))
    jira_issue_key: Mapped[str | None] = mapped_column(String(80))
    jira_project_key: Mapped[str | None] = mapped_column(String(64))
    jira_issue_type: Mapped[str | None] = mapped_column(String(64))
    retry_count: Mapped[int] = mapped_column(Integer, server_default="0")
    flow: Mapped[str | None] = mapped_column(String(16))
    verification_result: Mapped[str] = mapped_column(String(16))
    processing_result: Mapped[str] = mapped_column(String(32))
    ignore_reason: Mapped[str | None] = mapped_column(String(64))
    code_change_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("code_change_requests.id", ondelete="RESTRICT")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
