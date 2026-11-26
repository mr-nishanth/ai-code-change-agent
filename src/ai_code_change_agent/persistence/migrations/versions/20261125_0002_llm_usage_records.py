"""llm usage records: one row per LLM API call, for task and session cost reports

Append-only by convention and by trigger (no update, no delete). The call id is the primary key
and the provider message id is unique, so a replayed insert records nothing new.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-25 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("provider_message_id", sa.String(length=128), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_5m_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_1h_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=16, scale=10), nullable=True),
        sa.Column("long_context", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("inference_geo", sa.String(length=16), nullable=True),
        sa.Column("pricing_version", sa.String(length=64), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL) = (output_tokens IS NULL)",
            name=op.f("ck_llm_usage_records_usage_both_or_none"),
        ),
        sa.CheckConstraint(
            "cost_usd IS NULL OR input_tokens IS NOT NULL",
            name=op.f("ck_llm_usage_records_cost_needs_usage"),
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["code_change_attempts.id"],
            name=op.f("fk_llm_usage_records_attempt_id_code_change_attempts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_usage_records")),
        sa.UniqueConstraint(
            "provider_message_id", name=op.f("uq_llm_usage_records_provider_message_id")
        ),
    )
    op.create_index("ix_llm_usage_records_attempt_id", "llm_usage_records", ["attempt_id"])
    op.create_index("ix_llm_usage_records_session_id", "llm_usage_records", ["session_id"])
    op.execute(
        "CREATE TRIGGER llm_usage_records_append_only BEFORE UPDATE OR DELETE "
        "ON llm_usage_records FOR EACH ROW EXECUTE FUNCTION forbid_change()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS llm_usage_records_append_only ON llm_usage_records")
    op.drop_index("ix_llm_usage_records_session_id", table_name="llm_usage_records")
    op.drop_index("ix_llm_usage_records_attempt_id", table_name="llm_usage_records")
    op.drop_table("llm_usage_records")
