"""Durable job queue port. Adapter: `DynamoSqsJobQueue` (DynamoDB state, SQS delivery, outbox
relayed by the worker's dispatcher role)."""

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
    last_error: str | None = None  # the code a DEAD job ended with (on_dead hooks only)


class JobRetryableError(Exception):
    """A temporary failure: the job is retried with backoff while attempts remain.

    Any other exception is retried too. One that carries a positive `retry_after_seconds`
    attribute (for example `JiraRateLimitError`) makes the worker wait at least that long, up
    to its 900 s maximum backoff."""

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

    def release(self, job_id: uuid.UUID, owner: str) -> None:
        """Drop what this process keeps in memory for `owner`'s claim of the job (the SQS receipt).
        Never touches the stored job. Idempotent; the worker calls it once the job left its hands,
        whatever the outcome (completed, failed, lease lost)."""
        ...

    async def fail(
        self, job_id: uuid.UUID, owner: str, code: str, *, retry_in_seconds: int | None
    ) -> JobStatus:
        """Record a failure. With retry_in_seconds and attempts left the job is re-queued;
        otherwise it becomes DEAD. Returns the new status."""
        ...

    async def dispatch_pending(self) -> int:
        """Relay: deliver jobs whose creation committed but whose message was never sent (the
        transactional outbox). Returns how many were sent; a queue without an outbox returns 0."""
        ...

    async def sweep(self) -> int:
        """Re-deliver non-terminal jobs whose message was lost (overdue by more than a grace
        period). Returns how many were re-sent; a queue that cannot lose messages returns 0."""
        ...

    async def release_lease(self, job_id: uuid.UUID, owner: str, *, refund: bool) -> bool:
        """Give a claimed job back at once (worker shutdown): its lease ends now, so the next
        receipt reclaims it instead of waiting for the lease to run out. `refund` also returns
        the attempt the claim counted (the handler never started). False when `owner` no longer
        holds it."""
        ...

    async def dead_pending(self) -> list[Job]:
        """DEAD jobs whose kind's on_dead hook has not run yet, whichever path recorded them DEAD
        (the worker's last failure, or a claim that found the last attempt's lease expired), with
        `last_error` set. A job recorded DEAD by this worker's own `fail` is listed only after a
        grace period (the worker runs its hook at once)."""
        ...

    async def dead_settled(self, job_id: uuid.UUID, *, failed: bool = False) -> None:
        """The DEAD job's hook ran (or it has none): it leaves `dead_pending`. With `failed` the
        hook raised: it is listed again after a backoff, at most a bounded number of times."""
        ...
