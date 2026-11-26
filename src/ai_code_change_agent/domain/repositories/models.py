"""Registry entities and value-object validation. Pure Python: no framework imports."""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from ai_code_change_agent.domain.repositories.catalog import ACCESS_REFERENCE_SCHEMES
from ai_code_change_agent.domain.repositories.errors import Finding, RegistryValidationError

REPOSITORY_KEY_RE = re.compile(r"[a-z][a-z0-9-]{1,62}")
PROVIDER_RE = re.compile(r"[a-z][a-z0-9-]{1,31}")
PROVIDER_REPOSITORY_ID_RE = re.compile(r"[A-Za-z0-9_.:=-]{1,128}")
OWNER_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
DISPLAY_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{1,31}")
ACTOR_RE = re.compile(r"[a-z]{2,16}:[A-Za-z0-9_.@-]{1,64}")
_ACCESS_REFERENCE_RE = re.compile(
    "(" + "|".join(re.escape(s) for s in ACCESS_REFERENCE_SCHEMES) + r"):[A-Za-z0-9_./:@-]{1,200}"
)
# Values that look like credentials must never be stored or logged.
SECRET_LIKE_RE = re.compile(
    r"ghp_|gho_|ghs_|github_pat_|glpat-|xox[abpr]-|AKIA[0-9A-Z]{8,}|-----BEGIN|"
    r"password\s*=|token\s*=|secret\s*=|bearer\s",
    re.IGNORECASE,
)


class RepositoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class MappingStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class AuditAction(StrEnum):
    REPOSITORY_REGISTERED = "repository.registered"
    REPOSITORY_ENABLED = "repository.enabled"
    REPOSITORY_DISABLED = "repository.disabled"
    ALIAS_ADDED = "alias.added"
    PROFILE_VERSION_CREATED = "profile_version.created"
    PROFILE_VERSION_PUBLISHED = "profile_version.published"
    PROFILE_VERSION_ACTIVATED = "profile_version.activated"
    PROJECT_MAPPED = "project_mapping.activated"
    PROJECT_UNMAPPED = "project_mapping.disabled"


@dataclass(frozen=True)
class NewRepository:
    repository_key: str
    display_name: str
    git_provider: str
    provider_repository_id: str
    owner: str
    name: str
    default_branch: str
    access_reference: str

    @property
    def canonical_locator(self) -> str:
        return f"{self.git_provider}:{self.owner.lower()}/{self.name.lower()}"


@dataclass(frozen=True)
class Repository:
    id: uuid.UUID
    repository_key: str
    display_name: str
    git_provider: str
    provider_repository_id: str
    owner: str
    name: str
    canonical_locator: str
    default_branch: str
    status: RepositoryStatus
    access_reference: str
    active_profile_version_id: uuid.UUID | None
    row_version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ProfileVersion:
    id: uuid.UUID
    repository_id: uuid.UUID
    version: int
    schema_version: int
    content: dict[str, Any]
    content_sha256: str
    policy_version: str
    created_by: str
    created_at: datetime
    published_at: datetime | None

    @property
    def published(self) -> bool:
        return self.published_at is not None


@dataclass(frozen=True)
class ProjectMapping:
    id: uuid.UUID
    jira_project_key: str
    repository_id: uuid.UUID
    status: MappingStatus
    created_by: str
    created_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    action: AuditAction
    entity_type: str
    entity_id: uuid.UUID | None
    actor: str
    metadata: dict[str, str | int | bool | None] = field(default_factory=dict)
    correlation_id: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime | None = None


def _bad(findings: list[Finding], name: str, code: str, message: str) -> None:
    findings.append(Finding(name, code, message))


def validate_actor(actor: str) -> str:
    if not ACTOR_RE.fullmatch(actor):
        raise RegistryValidationError(
            [Finding("actor", "actor_invalid", "actor must look like kind:name")]
        )
    return actor


def validate_project_key(project_key: str) -> str:
    if not PROJECT_KEY_RE.fullmatch(project_key):
        raise RegistryValidationError(
            [Finding("jira_project_key", "project_key_invalid", "not a valid Jira project key")]
        )
    return project_key


def normalise_alias(alias: str) -> str:
    candidate = alias.strip().lower()
    if not REPOSITORY_KEY_RE.fullmatch(candidate):
        raise RegistryValidationError(
            [Finding("alias", "alias_invalid", "alias must be a lower-case slug (a-z, 0-9, -)")]
        )
    return candidate


def safe_metadata(values: dict[str, Any]) -> dict[str, str | int | bool | None]:
    """Audit metadata: short scalars only, never credential-looking values."""
    clean: dict[str, str | int | bool | None] = {}
    for key, value in values.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
            raise ValueError("unsafe audit metadata key")
        if value is not None and not isinstance(value, str | int | bool):
            raise ValueError("audit metadata must be scalar")
        if isinstance(value, str) and (len(value) > 200 or SECRET_LIKE_RE.search(value)):
            raise ValueError("unsafe audit metadata value")
        clean[key] = value
    return clean


def new_repository_findings(new: NewRepository) -> list[Finding]:
    findings: list[Finding] = []
    if not REPOSITORY_KEY_RE.fullmatch(new.repository_key):
        _bad(findings, "repository_key", "key_invalid", "lower-case slug, 2-63 chars")
    if not DISPLAY_NAME_RE.fullmatch(new.display_name):
        _bad(findings, "display_name", "display_name_invalid", "1-100 printable characters")
    if not PROVIDER_RE.fullmatch(new.git_provider):
        _bad(findings, "git_provider", "provider_invalid", "lower-case provider identifier")
    if not PROVIDER_REPOSITORY_ID_RE.fullmatch(new.provider_repository_id):
        _bad(findings, "provider_repository_id", "provider_id_invalid", "1-128 safe characters")
    for name, value in (("owner", new.owner), ("name", new.name)):
        if not OWNER_NAME_RE.fullmatch(value) or value in (".", "..") or value.endswith(".git"):
            _bad(findings, name, f"{name}_invalid", "1-100 characters from A-Z a-z 0-9 . _ -")
    if (
        not BRANCH_RE.fullmatch(new.default_branch)
        or ".." in new.default_branch
        or new.default_branch.endswith(("/", ".lock"))
        or "//" in new.default_branch
    ):
        _bad(findings, "default_branch", "branch_invalid", "not a safe branch name")
    ref = new.access_reference
    if not _ACCESS_REFERENCE_RE.fullmatch(ref) or SECRET_LIKE_RE.search(ref):
        _bad(
            findings,
            "access_reference",
            "access_reference_invalid",
            "opaque reference only (app-installation:, secret:, parameter:), never a credential",
        )
    return findings
