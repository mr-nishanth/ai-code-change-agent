"""One row per LLM API call (append-only usage ledger; see application/costs)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_code_change_agent.persistence.models.base import Base


class LlmUsageRecordModel(Base):
    __tablename__ = "llm_usage_records"
    __table_args__ = (
        CheckConstraint(
            "(input_tokens IS NULL) = (output_tokens IS NULL)", name="usage_both_or_none"
        ),
        CheckConstraint("cost_usd IS NULL OR input_tokens IS NOT NULL", name="cost_needs_usage"),
        Index("ix_llm_usage_records_attempt_id", "attempt_id"),
        Index("ix_llm_usage_records_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # the call id (run id)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("code_change_attempts.id", ondelete="RESTRICT")
    )
    stage: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    provider_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    succeeded: Mapped[bool] = mapped_column(Boolean)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_5m_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_1h_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(16, 10))
    long_context: Mapped[bool] = mapped_column(Boolean, server_default="false")
    inference_geo: Mapped[str | None] = mapped_column(String(16))
    pricing_version: Mapped[str] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
