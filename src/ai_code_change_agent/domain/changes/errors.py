"""Typed domain errors. Messages are fixed codes; never include secrets or raw input."""

from ai_code_change_agent.domain.repositories.errors import ConflictError


class ChangeError(Exception):
    code = "change_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class ChangeValidationError(ChangeError):
    code = "validation_failed"


class ChangeNotFoundError(ChangeError):
    code = "not_found"


class ChangeConflictError(ChangeError):
    code = "conflict"


class ConcurrentModificationError(ChangeConflictError, ConflictError):
    """A lost optimistic race: an item this unit of work read changed before it committed.

    A subclass of both conflict errors (changes and registry), so existing handlers treat it as a
    conflict; services retry the whole method on this class only (DYNAMODB-DATA-MODEL.md 5.1)."""

    code = "concurrent_modification"


class InvalidTransitionError(ChangeError):
    code = "invalid_transition"


class ApprovalRequiredError(ChangeError):
    code = "approval_required"


class StaleApprovalError(ChangeError):
    code = "stale_approval"


class ActiveAttemptError(ChangeError):
    code = "active_attempt_exists"


class ImmutableAssignmentError(ChangeError):
    code = "assignment_immutable"
