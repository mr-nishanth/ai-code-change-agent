"""What the parent workflow needs from the application. Nodes hold no business logic: every
step is an application service call that persists its own state transition."""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

# DynamoDBSaver keys every checkpoint item by its thread id; DM 9.2 budgets item metadata for ids
# under 200 characters, so the payload threshold keeps its headroom under the 400 KB item limit.
MAX_THREAD_ID_LENGTH = 200


def checked_thread_id(thread_id: str) -> str:
    """`thread_id` unchanged when it is shorter than MAX_THREAD_ID_LENGTH, else ValueError."""
    if len(thread_id) >= MAX_THREAD_ID_LENGTH:
        msg = f"thread id is {len(thread_id)} characters; must be under {MAX_THREAD_ID_LENGTH}"
        raise ValueError(msg)
    return thread_id


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
