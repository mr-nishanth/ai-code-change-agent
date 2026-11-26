"""Repository registry tables (revision 0004)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_code_change_agent.persistence.models.base import Base

REPOSITORY_STATUSES = ("ACTIVE", "DISABLED")
MAPPING_STATUSES = ("ACTIVE", "DISABLED")
ALIAS_KINDS = ("KEY", "ALIAS")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class RepositoryModel(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint(
            "git_provider", "provider_repository_id", name="uq_repositories_provider_repository"
        ),
        CheckConstraint(_in("status", REPOSITORY_STATUSES), name="status_valid"),
        CheckConstraint("repository_key ~ '^[a-z][a-z0-9-]{1,62}$'", name="key_format"),
        CheckConstraint("git_provider ~ '^[a-z][a-z0-9-]{1,31}$'", name="provider_format"),
        CheckConstraint(
            "status <> 'ACTIVE' OR active_profile_version_id IS NOT NULL",
            name="active_requires_profile",
        ),
        ForeignKeyConstraint(
            ["id", "active_profile_version_id"],
            ["repository_profile_versions.repository_id", "repository_profile_versions.id"],
            name="fk_repositories_active_profile_version",
            use_alter=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_key: Mapped[str] = mapped_column(String(63), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    git_provider: Mapped[str] = mapped_column(String(32))
    provider_repository_id: Mapped[str] = mapped_column(String(128))
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    canonical_locator: Mapped[str] = mapped_column(String(255), unique=True)
    default_branch: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), server_default="DISABLED")
    access_reference: Mapped[str] = mapped_column(String(255))
    active_profile_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepositoryProfileVersionModel(Base):
    """Immutable once published; a database trigger forbids any other change and all deletes."""

    __tablename__ = "repository_profile_versions"
    __table_args__ = (
        UniqueConstraint("repository_id", "version", name="uq_repository_profile_versions_number"),
        UniqueConstraint("repository_id", "id", name="uq_repository_profile_versions_repo_id"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repositories.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_sha256: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RepositoryAliasModel(Base):
    """One global namespace for repository keys (kind KEY) and aliases: no collisions possible."""

    __tablename__ = "repository_aliases"
    __table_args__ = (
        CheckConstraint(_in("kind", ALIAS_KINDS), name="kind_valid"),
        CheckConstraint("alias ~ '^[a-z][a-z0-9-]{1,62}$'", name="alias_format"),
        Index("ix_repository_aliases_repository_id", "repository_id"),
    )

    alias: Mapped[str] = mapped_column(String(63), primary_key=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repositories.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(8))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepositoryProjectMappingModel(Base):
    """A Jira project is authorised to target a repository only through an ACTIVE mapping."""

    __tablename__ = "repository_project_mappings"
    __table_args__ = (
        UniqueConstraint(
            "jira_project_key", "repository_id", name="uq_repository_project_mappings_pair"
        ),
        CheckConstraint(_in("status", MAPPING_STATUSES), name="status_valid"),
        CheckConstraint("jira_project_key ~ '^[A-Z][A-Z0-9_]{1,31}$'", name="project_key_format"),
        Index("ix_repository_project_mappings_project", "jira_project_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    jira_project_key: Mapped[str] = mapped_column(String(32))
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("repositories.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), server_default="ACTIVE")
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RegistryAuditEventModel(Base):
    """Append-only (database trigger). Metadata holds safe scalars only, never credentials."""

    __tablename__ = "registry_audit_events"
    __table_args__ = (
        Index("ix_registry_audit_events_entity", "entity_type", "entity_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    event_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, server_default="{}")
