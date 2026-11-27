"""Worker behaviour over an in-memory queue double."""

import asyncio
import uuid
from typing import Any

import pytest

from ai_code_change_agent.application.jira.errors import JiraRateLimitError
from ai_code_change_agent.application.jobs.queue import (
    Job,
    JobFailedError,
    JobRetryableError,
    JobStatus,
)
from ai_code_change_agent.application.jobs.worker import Worker, backoff_seconds


class MemoryQueue:
    def __init__(self, jobs: list[Job] | None = None, *, keep_lease: bool = True) -> None:
        self.jobs = list(jobs or [])
        self.keep_lease = keep_lease
        self.completed: list[uuid.UUID] = []
        self.failed: list[tuple[uuid.UUID, str, int | None]] = []
        self.beats = 0
        self.relays = 0
        self.sweeps = 0

    async def enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str, max_attempts: int
    ) -> bool:
        self.jobs.append(Job(uuid.uuid4(), kind, payload, 0, max_attempts))
        return True

    async def claim(self, owner: str, lease_seconds: int) -> Job | None:
        return self.jobs.pop(0) if self.jobs else None

    async def heartbeat(self, job_id: uuid.UUID, owner: str, lease_seconds: int) -> bool:
        self.beats += 1
        return self.keep_lease

    async def complete(self, job_id: uuid.UUID, owner: str) -> bool:
        self.completed.append(job_id)
        return True

    async def fail(
        self, job_id: uuid.UUID, owner: str, code: str, *, retry_in_seconds: int | None
    ) -> JobStatus:
        self.failed.append((job_id, code, retry_in_seconds))
        return JobStatus.QUEUED if retry_in_seconds is not None else JobStatus.DEAD

    async def dispatch_pending(self) -> int:
        self.relays += 1
        return 0

    async def sweep(self) -> int:
        self.sweeps += 1
        return 0


def job(kind: str = "k.one", attempts: int = 1) -> Job:
    return Job(uuid.uuid4(), kind, {}, attempts, 3)


def worker(queue: MemoryQueue, handlers: dict[str, Any], beat: float = 30) -> Worker:
    return Worker(queue, handlers, lease_seconds=60, heartbeat_seconds=beat, poll_seconds=0.01)


async def test_success_completes_the_job() -> None:
    seen: list[Job] = []

    async def ok(j: Job) -> None:
        seen.append(j)

    q = MemoryQueue([job()])
    assert await worker(q, {"k.one": ok}).run_once() is not None
    assert len(seen) == 1 and len(q.completed) == 1 and q.failed == []
    assert await worker(q, {"k.one": ok}).run_once() is None  # nothing left


@pytest.mark.parametrize(
    ("exc", "code", "retry"),
    [(JobRetryableError("jira_unavailable"), "jira_unavailable", 30),
     (RuntimeError("raw text never stored"), "RuntimeError", 30),
     (JobFailedError("repository_unknown"), "repository_unknown", None)],
)  # fmt: skip
async def test_failures_are_classified(exc: Exception, code: str, retry: int | None) -> None:
    async def bad(j: Job) -> None:
        raise exc

    q = MemoryQueue([job()])
    await worker(q, {"k.one": bad}).run_once()
    assert q.failed[0][1:] == (code, retry) and q.completed == []


async def test_an_unknown_kind_is_dead_at_once() -> None:
    q = MemoryQueue([job("k.unknown")])
    await worker(q, {}).run_once()
    assert q.failed[0][1:] == ("no_handler", None)


async def test_a_lost_lease_cancels_the_handler_and_records_nothing() -> None:
    started = asyncio.Event()

    async def slow(j: Job) -> None:
        started.set()
        await asyncio.sleep(10)

    q = MemoryQueue([job()], keep_lease=False)
    await worker(q, {"k.one": slow}, beat=0.01).run_once()
    assert started.is_set() and q.completed == [] and q.failed == [] and q.beats == 1


async def test_heartbeats_keep_long_jobs_alive() -> None:
    async def medium(j: Job) -> None:
        await asyncio.sleep(0.05)

    q = MemoryQueue([job()])
    await worker(q, {"k.one": medium}, beat=0.01).run_once()
    assert q.beats >= 2 and len(q.completed) == 1


async def test_run_loop_respects_concurrency_and_stops() -> None:
    running = 0
    peak = 0

    async def handler(j: Job) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1

    q = MemoryQueue([job() for _ in range(6)])
    w = Worker(
        q,
        {"k.one": handler},
        lease_seconds=60,
        heartbeat_seconds=30,
        concurrency=2,
        poll_seconds=0.01,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    while len(q.completed) < 6:  # noqa: ASYNC110 - polling a test double
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert peak == 2 and len(q.completed) == 6


class _RetryAfter(Exception):  # noqa: N818 - mirrors JiraRateLimitError's attribute only
    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("raw text never stored")
        self.retry_after_seconds = retry_after_seconds


@pytest.mark.parametrize(
    ("exc", "retry"),
    [(_RetryAfter(120), 120),
     (_RetryAfter(5000), 900),  # capped: a longer Retry-After may exhaust the job's attempts
     (_RetryAfter(10), 30),
     (JobRetryableError("jira_rate_limited"), 30),
     (_RetryAfter(120.2), 121),
     (_RetryAfter(0), 30),
     (_RetryAfter(-5), 30),
     (_RetryAfter(None), 30)],
)  # fmt: skip
async def test_a_retry_after_hint_lengthens_the_backoff_up_to_the_cap(
    exc: Exception, retry: int
) -> None:
    async def bad(j: Job) -> None:
        raise exc

    q = MemoryQueue([job()])
    await worker(q, {"k.one": bad}).run_once()
    assert q.failed[0][2] == retry


async def test_a_jira_rate_limit_carries_its_retry_after_into_the_queue() -> None:
    async def limited(j: Job) -> None:
        raise JiraRateLimitError(operation="get_issue", status_code=429, retry_after_seconds=120)

    q = MemoryQueue([job()])
    await worker(q, {"k.one": limited}).run_once()
    assert q.failed[0][1:] == ("JiraRateLimitError", 120)


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert backoff_seconds(30) == 900


class FlakyQueue(MemoryQueue):
    """Claims and dispatcher steps fail first; the worker keeps going."""

    def __init__(self, jobs: list[Job]) -> None:
        super().__init__(jobs)
        self.claims = 0

    async def claim(self, owner: str, lease_seconds: int) -> Job | None:
        self.claims += 1
        if self.claims == 1:
            raise JobRetryableError("store_unavailable")
        return await super().claim(owner, lease_seconds)

    async def dispatch_pending(self) -> int:
        self.relays += 1
        if self.relays == 1:
            raise RuntimeError("raw text never logged")
        return 2

    async def sweep(self) -> int:
        self.sweeps += 1
        raise RuntimeError("raw text never logged")

    async def complete(self, job_id: uuid.UUID, owner: str) -> bool:
        if not self.completed:
            self.completed.append(job_id)
            raise RuntimeError("recording failed")
        return await super().complete(job_id, owner)


async def test_the_dispatcher_relays_every_poll_and_sweeps_on_its_own_interval() -> None:
    q = MemoryQueue()
    w = Worker(q, {}, lease_seconds=60, heartbeat_seconds=30, poll_seconds=0.01, sweep_seconds=0.05)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.sleep(0.2)
    stop.set()
    await task
    assert q.relays >= 5 and 2 <= q.sweeps < q.relays


async def test_claim_dispatcher_and_recording_failures_never_stop_the_worker() -> None:
    async def ok(j: Job) -> None:
        return None

    q = FlakyQueue([job(), job()])
    w = Worker(q, {"k.one": ok}, lease_seconds=60, heartbeat_seconds=30, poll_seconds=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    while len(q.completed) < 2 or q.relays < 3:  # noqa: ASYNC110 - polling a test double
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert q.claims >= 3 and q.sweeps >= 1
