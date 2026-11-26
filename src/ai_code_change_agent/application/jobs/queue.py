"""Durable job queue port. The PostgreSQL adapter is the production implementation."""

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    DEAD = "DEAD"


@dataclass(frozen=True)
class Job:
    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int  # including the current one
    max_attempts: int


class JobRetryableError(Exception):
    """A temporary failure: the job is retried with backoff while attempts remain."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class JobFailedError(Exception):
    """A permanent failure: the job is not retried."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class JobQueue(Protocol):
    async def enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str, max_attempts: int
    ) -> bool:
        """True when a new job was created; False when the dedupe key already exists."""
        ...

    async def claim(self, owner: str, lease_seconds: int) -> Job | None:
        """Claim one runnable job: queued and due, or running with an expired lease (a crashed
        worker). Jobs whose attempts are exhausted are never claimed again."""
        ...

    async def heartbeat(self, job_id: uuid.UUID, owner: str, lease_seconds: int) -> bool:
        """Extend the lease. False means the lease was lost (another worker may own it)."""
        ...

    async def complete(self, job_id: uuid.UUID, owner: str) -> bool: ...

    async def fail(
        self, job_id: uuid.UUID, owner: str, code: str, *, retry_in_seconds: int | None
    ) -> JobStatus:
        """Record a failure. With retry_in_seconds and attempts left the job is re-queued;
        otherwise it becomes DEAD. Returns the new status."""
        ...
