"""Typed outcomes of deterministic repository resolution."""

import uuid
from dataclasses import dataclass
from enum import StrEnum


class ResolutionOutcome(StrEnum):
    RESOLVED = "RESOLVED"
    MISSING_REFERENCE = "MISSING_REFERENCE"
    UNKNOWN_REPOSITORY = "UNKNOWN_REPOSITORY"
    AMBIGUOUS_REFERENCE = "AMBIGUOUS_REFERENCE"
    DISABLED_REPOSITORY = "DISABLED_REPOSITORY"
    PROJECT_NOT_AUTHORISED = "PROJECT_NOT_AUTHORISED"
    PROFILE_MISSING = "PROFILE_MISSING"
    PROFILE_INVALID = "PROFILE_INVALID"


class FollowUp(StrEnum):
    NONE = "NONE"
    CLARIFICATION = "CLARIFICATION"
    OPERATIONS_REVIEW = "OPERATIONS_REVIEW"


FOLLOW_UP: dict[ResolutionOutcome, FollowUp] = {
    ResolutionOutcome.RESOLVED: FollowUp.NONE,
    ResolutionOutcome.MISSING_REFERENCE: FollowUp.CLARIFICATION,
    ResolutionOutcome.UNKNOWN_REPOSITORY: FollowUp.CLARIFICATION,
    ResolutionOutcome.AMBIGUOUS_REFERENCE: FollowUp.CLARIFICATION,
    ResolutionOutcome.DISABLED_REPOSITORY: FollowUp.OPERATIONS_REVIEW,
    ResolutionOutcome.PROJECT_NOT_AUTHORISED: FollowUp.OPERATIONS_REVIEW,
    ResolutionOutcome.PROFILE_MISSING: FollowUp.OPERATIONS_REVIEW,
    ResolutionOutcome.PROFILE_INVALID: FollowUp.OPERATIONS_REVIEW,
}


@dataclass(frozen=True)
class ResolutionRequest:
    """Untrusted inputs from a Jira Story: the project and the configured reference value(s)."""

    issue_key: str
    project_key: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolutionResult:
    outcome: ResolutionOutcome
    detail: str
    repository_id: uuid.UUID | None = None
    repository_key: str | None = None
    profile_version_id: uuid.UUID | None = None
    profile_version: int | None = None
    profile_sha256: str | None = None

    @property
    def resolved(self) -> bool:
        return self.outcome is ResolutionOutcome.RESOLVED

    @property
    def follow_up(self) -> FollowUp:
        return FOLLOW_UP[self.outcome]
