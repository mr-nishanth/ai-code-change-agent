"""Typed domain errors. Messages are fixed codes; never include secrets or raw input."""


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


class InvalidTransitionError(ChangeError):
    code = "invalid_transition"


class ApprovalRequiredError(ChangeError):
    code = "approval_required"


class StaleApprovalError(ChangeError):
    code = "stale_approval"


class ActiveAttemptError(ChangeError):
    code = "active_attempt_exists"


class ReprocessNotAllowedError(ChangeError):
    code = "reprocess_not_allowed"


class ImmutableAssignmentError(ChangeError):
    code = "assignment_immutable"


class NotAuthorisedError(ChangeError):
    code = "not_authorised"
