"""Application errors shared by services and workflow nodes. Messages are fixed codes."""


class WorkflowError(Exception):
    code = "workflow_error"

    def __init__(self, code: str | None = None) -> None:
        if code is not None:
            self.code = code
        super().__init__(self.code)


class WorkflowPreconditionError(WorkflowError):
    """A required precondition does not hold (for example the repository is not authorised)."""

    code = "precondition_failed"


class ArtifactIntegrityError(WorkflowError):
    code = "artifact_integrity_failed"
