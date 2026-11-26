"""Durable Jira reporting lifecycle for multi-repository parents.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REPORT_STATES = (
    "('PENDING', 'IN_PROGRESS', 'SUCCEEDED', 'RETRYABLE_FAILURE', 'PERMANENT_FAILURE', 'DISABLED')"
)


def upgrade() -> None:
    op.alter_column(
        "code_change_parents",
        "jira_report",
        type_=sa.String(length=24),
        existing_type=sa.String(length=16),
        existing_nullable=True,
    )
    op.add_column("code_change_parents", sa.Column("jira_comment_id", sa.String(length=32)))
    op.add_column(
        "code_change_parents",
        sa.Column("jira_report_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("code_change_parents", sa.Column("jira_report_error", sa.String(length=120)))
    op.add_column("code_change_parents", sa.Column("jira_report_at", sa.DateTime(timezone=True)))
    # Old markers: POSTED meant Jira answered 201; POSTING was a claim a crash could strand.
    op.execute(
        "UPDATE code_change_parents SET jira_report = 'SUCCEEDED', jira_report_attempts = 1 "
        "WHERE jira_report = 'POSTED'"
    )
    op.execute(
        "UPDATE code_change_parents SET jira_report = 'IN_PROGRESS', jira_report_attempts = 1, "
        "jira_report_at = updated_at WHERE jira_report = 'POSTING'"
    )
    op.create_check_constraint(
        "jira_report_valid",
        "code_change_parents",
        f"jira_report IS NULL OR jira_report IN {REPORT_STATES}",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_code_change_parents_jira_report_valid"), "code_change_parents")
    op.execute(
        "UPDATE code_change_parents SET jira_report = CASE jira_report "
        "WHEN 'SUCCEEDED' THEN 'POSTED' WHEN 'IN_PROGRESS' THEN 'POSTING' ELSE NULL END"
    )
    for column in (
        "jira_report_at",
        "jira_report_error",
        "jira_report_attempts",
        "jira_comment_id",
    ):
        op.drop_column("code_change_parents", column)
    op.alter_column(
        "code_change_parents",
        "jira_report",
        type_=sa.String(length=16),
        existing_type=sa.String(length=24),
        existing_nullable=True,
    )
