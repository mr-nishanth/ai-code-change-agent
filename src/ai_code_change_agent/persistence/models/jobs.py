"""Durable job queue (PostgreSQL). Claimed with FOR UPDATE SKIP LOCKED; leases and heartbeats
let a crashed worker's job be reclaimed; attempts are bounded and exhausted jobs become DEAD."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, Uuid, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_code_change_agent.persistence.models.base import Base

JOB_STATUSES = ("QUEUED", "RUNNING", "SUCCEEDED", "DEAD")


class JobModel(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in JOB_STATUSES)})", name="status_valid"
        ),
        CheckConstraint("kind ~ '^[a-z][a-z0-9_.]{2,63}$'", name="kind_format"),
        CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="attempts_bounded"),
        CheckConstraint("max_attempts BETWEEN 1 AND 10", name="max_attempts_range"),
        CheckConstraint(
            "status <> 'RUNNING' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        Index(
            "ix_jobs_claimable",
            "available_at",
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(16), server_default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="3")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
