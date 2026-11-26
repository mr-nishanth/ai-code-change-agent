"""What the parent workflow needs from the application. Nodes hold no business logic: every
step is an application service call that persists its own state transition."""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class GateState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class StepResult:
    status: str  # the attempt status after the step (AttemptStatus value)
    feedback: str | None = None
    failure_code: str | None = None


class WorkflowSteps(Protocol):
    async def prepare(self, attempt_id: uuid.UUID) -> StepResult: ...
    async def plan(self, attempt_id: uuid.UUID) -> StepResult: ...
    async def gate(self, attempt_id: uuid.UUID, gate: str) -> GateState: ...
    async def implement(self, attempt_id: uuid.UUID, feedback: str | None) -> StepResult: ...
    async def validate(self, attempt_id: uuid.UUID) -> StepResult: ...
    async def deliver(self, attempt_id: uuid.UUID) -> StepResult: ...
    async def report(self, attempt_id: uuid.UUID) -> StepResult: ...
    async def cleanup(self, attempt_id: uuid.UUID) -> None: ...
