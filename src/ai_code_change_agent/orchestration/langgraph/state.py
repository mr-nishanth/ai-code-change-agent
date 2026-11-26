"""Parent workflow state. Only identifiers and short codes live here; artifacts and decisions are
in PostgreSQL (domain tables) and the artifact store, which stay the source of truth."""

from typing import NotRequired, TypedDict


class WorkflowState(TypedDict):
    attempt_id: str
    status: NotRequired[str]  # the attempt status after the last step
    feedback: NotRequired[str | None]  # validation feedback for a bounded repair
    failure_code: NotRequired[str | None]
