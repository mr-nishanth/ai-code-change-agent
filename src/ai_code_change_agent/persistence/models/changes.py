"""Attempts and append-only approvals. Database triggers (revision 0001) keep the pinned base
commit immutable, forbid deleting attempts and make approvals append-only."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_code_change_agent.domain.changes.states import TERMINAL, AttemptStatus
from ai_code_change_agent.persistence.models.base import Base

_STATUSES = ", ".join(repr(s.value) for s in AttemptStatus)
_TERMINAL = ", ".join(repr(s.value) for s in sorted(TERMINAL))


class CodeChangeAttemptModel(Base):
    __tablename__ = "code_change_attempts"
    __table_args__ = (
        CheckConstraint(f"status IN ({_STATUSES})", name="status_valid"),
        CheckConstraint("number >= 1", name="number_positive"),
        CheckConstraint("repair_count BETWEEN 0 AND 10", name="repair_count_range"),
        CheckConstraint(
            "base_commit_sha IS NULL OR base_commit_sha ~ '^[0-9a-f]{40}$'", name="base_commit_hex"
        ),
        CheckConstraint(
            "plan_sha256 IS NULL OR plan_sha256 ~ '^[0-9a-f]{64}$'", name="plan_sha256_hex"
        ),
        CheckConstraint(
            "manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="manifest_sha256_hex",
        ),
        CheckConstraint(
            "status NOT IN ('FAILED', 'BLOCKED') OR failure_code IS NOT NULL",
            name="failure_requires_code",
        ),
        UniqueConstraint("request_id", "number", name="uq_code_change_attempts_number"),
        Index(
            "uq_code_change_attempts_one_active",
            "request_id",
            "repository_id",  # one active attempt per repository of a request (multi-repo)
            unique=True,
            postgresql_where=text(f"status NOT IN ({_TERMINAL})"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("code_change_requests.id", ondelete="RESTRICT")
    )
    number: Mapped[int] = mapped_column(Integer)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repositories.id", ondelete="RESTRICT")
    )
    profile_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repository_profile_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(32))
    base_commit_sha: Mapped[str | None] = mapped_column(String(40))
    plan_ref: Mapped[str | None] = mapped_column(String(64))
    plan_sha256: Mapped[str | None] = mapped_column(String(64))
    manifest_ref: Mapped[str | None] = mapped_column(String(64))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    repair_count: Mapped[int] = mapped_column(Integer, server_default="0")
    failure_code: Mapped[str | None] = mapped_column(String(64))
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CodeChangeApprovalModel(Base):
    __tablename__ = "code_change_approvals"
    __table_args__ = (
        CheckConstraint("gate IN ('PLAN', 'FINAL')", name="gate_valid"),
        CheckConstraint("decision IN ('APPROVED', 'REJECTED')", name="decision_valid"),
        CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="artifact_sha256_hex"),
        Index("ix_code_change_approvals_attempt", "attempt_id", "gate", "decided_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("code_change_attempts.id", ondelete="RESTRICT")
    )
    gate: Mapped[str] = mapped_column(String(8))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    actor: Mapped[str] = mapped_column(String(100))
    actor_verified: Mapped[bool] = mapped_column(Boolean, server_default="false")
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
