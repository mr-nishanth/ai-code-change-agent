"""multi-repository executions: one parent per Story, one child per repository

* code_change_parents: one row per Jira request that declares several repositories;
* code_change_children: one row per declared repository (role, dependencies, contract, PR);
* at most one non-terminal attempt per (request, repository) instead of per request, so the
  children of one Story can run side by side; single-repository Stories are unchanged;
* requests may report PARTIALLY_COMPLETED.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-26 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERMINAL = "('BLOCKED', 'CANCELLED', 'COMPLETED', 'FAILED', 'REJECTED')"
_REQUEST_STATES = (
    "'RECEIVED', 'PENDING', 'PREPARING', 'PLANNING', 'AWAITING_PLAN_APPROVAL', 'IMPLEMENTING', "
    "'VALIDATING', 'AWAITING_FINAL_APPROVAL', 'DELIVERING', 'REPORTING', 'COMPLETED', 'BLOCKED', "
    "'FAILED', 'CANCELLED', 'REJECTED'"
)
PARENT_STATES = "('RUNNING', 'COMPLETED', 'PARTIALLY_COMPLETED', 'FAILED', 'BLOCKED')"
CHILD_STATES = "('WAITING', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'REJECTED', 'BLOCKED')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_code_change_requests_status_valid"), "code_change_requests")
    op.create_check_constraint(
        "status_valid",
        "code_change_requests",
        f"status IN ({_REQUEST_STATES}, 'PARTIALLY_COMPLETED')",
    )
    op.drop_index("uq_code_change_attempts_one_active", table_name="code_change_attempts")
    op.create_index(
        "uq_code_change_attempts_one_active",
        "code_change_attempts",
        ["request_id", "repository_id"],
        unique=True,
        postgresql_where=sa.text(f"status NOT IN {_TERMINAL}"),
    )
    op.create_table(
        "code_change_parents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("declaration", postgresql.JSONB(), nullable=False),
        sa.Column("report_round", sa.Integer(), server_default="0", nullable=False),
        sa.Column("jira_report", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"status IN {PARENT_STATES}", name=op.f("ck_code_change_parents_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["code_change_requests.id"],
            name=op.f("fk_code_change_parents_request_id_code_change_requests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_change_parents")),
        sa.UniqueConstraint("request_id", name=op.f("uq_code_change_parents_request_id")),
    )
    op.create_table(
        "code_change_children",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("repository_key", sa.String(length=63), nullable=False),
        sa.Column("reference", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=True),
        sa.Column("responsibility", sa.Text(), nullable=True),
        sa.Column("depends_on", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("runs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("contract_ref", sa.String(length=64), nullable=True),
        sa.Column("contract_sha256", sa.String(length=64), nullable=True),
        sa.Column("pr_url", sa.String(length=300), nullable=True),
        sa.Column("branch", sa.String(length=120), nullable=True),
        sa.Column("failure_code", sa.String(length=120), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN {CHILD_STATES}", name=op.f("ck_code_change_children_status_valid")
        ),
        sa.CheckConstraint(
            "contract_sha256 IS NULL OR contract_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_code_change_children_contract_sha256_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["code_change_parents.id"],
            name=op.f("fk_code_change_children_parent_id_code_change_parents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_code_change_children_repository_id_repositories"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["code_change_attempts.id"],
            name=op.f("fk_code_change_children_attempt_id_code_change_attempts"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_change_children")),
        sa.UniqueConstraint(
            "parent_id", "repository_id", name=op.f("uq_code_change_children_parent_id")
        ),
        sa.UniqueConstraint("parent_id", "position", name=op.f("uq_code_change_children_position")),
    )
    op.create_index("ix_code_change_children_attempt_id", "code_change_children", ["attempt_id"])
    for table in ("code_change_parents", "code_change_children"):
        op.execute(
            f"CREATE TRIGGER {table}_never_deleted BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_change()"
        )


def downgrade() -> None:
    for table in ("code_change_children", "code_change_parents"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_never_deleted ON {table}")
    op.drop_index("ix_code_change_children_attempt_id", table_name="code_change_children")
    op.drop_table("code_change_children")
    op.drop_table("code_change_parents")
    op.drop_index("uq_code_change_attempts_one_active", table_name="code_change_attempts")
    op.create_index(
        "uq_code_change_attempts_one_active",
        "code_change_attempts",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text(f"status NOT IN {_TERMINAL}"),
    )
    op.drop_constraint(op.f("ck_code_change_requests_status_valid"), "code_change_requests")
    op.create_check_constraint(
        "status_valid",
        "code_change_requests",
        f"status IN ({_REQUEST_STATES})",
    )
