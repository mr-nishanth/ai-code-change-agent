"""Artifact identity, scope and metadata."""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

ARTIFACT_ID = re.compile(r"art_[0-9a-f]{32}")


class ArtifactKind(StrEnum):
    REPOSITORY_SNAPSHOT = "repository_snapshot"  # pinned baseline: paths and hashes only
    REPOSITORY_DEPENDENCIES = "repository_dependencies"  # discovered manifest dependencies
    IMPLEMENTATION_PLAN = "implementation_plan"  # planning phase output; approved by hash
    CHANGESET = "changeset"  # the files the implementation phase changed, with content
    IMPLEMENTATION_REPORT = "implementation_report"  # the agent's own account (never authoritative)
    VALIDATION_REPORT = "validation_report"  # the independent validation engine's decision
    VALIDATION_EVIDENCE = "validation_evidence"
    DELIVERY_MANIFEST = "delivery_manifest"  # approved by hash before delivery
    DELIVERY_RESULT = "delivery_result"
    DELIVERY_PROGRESS = "delivery_progress"
    DEPENDENCY_LOCK = "dependency_lock"  # the verified bundle the attempt ran with (ADR-010)
    AUTOMATIC_DECISION = "automatic_decision"  # policy result behind an automatic gate decision
    JIRA_REPORT = "jira_report"  # marks the outcome comment as posted (idempotent reporting)
    CROSS_REPO_CONTRACT = "cross_repo_contract"  # an upstream child's published interface


@dataclass(frozen=True)
class ArtifactScope:
    """One request and one attempt own an artifact; lookups outside that scope find nothing."""

    request_id: uuid.UUID
    attempt_id: uuid.UUID


@dataclass(frozen=True)
class ArtifactMeta:
    id: str  # opaque: art_<32 hex>, never derived from caller input
    kind: ArtifactKind
    scope: ArtifactScope
    sha256: str  # of the stored bytes (after redaction, if any was applied)
    size: int
    created_at: datetime
    redaction_version: str
    redactions: int  # number of values replaced before storage (0 for forbid-policy kinds)
