"""Worker behaviour over an in-memory queue double."""

import asyncio
import uuid
from typing import Any

import pytest
from structlog.testing import capture_logs

from ai_code_change_agent.application.jira.errors import JiraRateLimitError
from ai_code_change_agent.application.jobs.queue import (
    Job,
    JobFailedError,
    JobRetryableError,
    JobStatus,
)
from ai_code_change_agent.application.jobs.worker import Worker, backoff_seconds
from ai_code_change_agent.domain.webhooks.models import PersistenceError


class MemoryQueue:
    def __init__(self, jobs: list[Job] | None = None, *, keep_lease: bool = True) -> None:
        self.jobs = list(jobs or [])
        self.keep_lease = keep_lease
        self.completed: list[uuid.UUID] = []
        self.failed: list[tuple[uuid.UUID, str, int | None]] = []
        self.beats = 0
        self.relays = 0
        self.sweeps = 0
        self.released: list[uuid.UUID] = []

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

    def release(self, job_id: uuid.UUID, owner: str) -> None:
        self.released.append(job_id)

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


async def test_a_claimed_job_is_logged_with_its_attempt_and_never_its_payload() -> None:
    async def ok(j: Job) -> None:
        return None

    claimed = Job(uuid.uuid4(), "k.one", {"api_token": "s3cret-value", "story": "text"}, 2, 3)
    q = MemoryQueue([claimed])
    with capture_logs() as logs:
        await worker(q, {"k.one": ok}).run_once()
    (line,) = [e for e in logs if e["event"] == "job_claimed"]
    assert line == {
        "event": "job_claimed",
        "log_level": "info",
        "job_id": str(claimed.id),
        "kind": "k.one",
        "attempt": 2,
        "max_attempts": 3,
    }
    assert "s3cret-value" not in repr(logs) and "story" not in repr(logs)


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


async def test_the_dispatcher_relays_on_its_interval_and_sweeps_on_its_own() -> None:
    q = MemoryQueue()
    w = Worker(
        q, {}, lease_seconds=60, heartbeat_seconds=30, poll_seconds=5, relay_seconds=0.01,
        sweep_seconds=0.05,
    )  # fmt: skip
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
    w = Worker(
        q, {"k.one": ok}, lease_seconds=60, heartbeat_seconds=30, poll_seconds=0.01,
        relay_seconds=0.01,
    )  # fmt: skip
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    while len(q.completed) < 2 or q.relays < 3:  # noqa: ASYNC110 - polling a test double
        await asyncio.sleep(0.01)
    stop.set()
    await task
    assert q.claims >= 3 and q.sweeps >= 1


class BeatScriptQueue(MemoryQueue):
    """Heartbeats follow a script: an exception instance is raised, a bool is returned; the last
    entry repeats once the script runs out."""

    def __init__(self, script: list[bool | Exception]) -> None:
        super().__init__([job()])
        self.script = script

    async def heartbeat(self, job_id: uuid.UUID, owner: str, lease_seconds: int) -> bool:
        self.beats += 1
        step = self.script[min(self.beats - 1, len(self.script) - 1)]
        if isinstance(step, Exception):
            raise step
        return step


async def test_a_transient_heartbeat_failure_is_retried_and_the_job_completes() -> None:
    async def medium(j: Job) -> None:
        await asyncio.sleep(0.2)

    q = BeatScriptQueue([PersistenceError("EndpointConnectionError"), True])
    await worker(q, {"k.one": medium}, beat=0.02).run_once()
    assert q.beats >= 3 and len(q.completed) == 1 and q.failed == []


async def test_heartbeat_failures_never_escape_into_the_completion_path() -> None:
    """Regression for the S2116 review probe: every heartbeat raises, the handler finishes well
    inside the lease, and the job is still completed instead of process() raising."""
    done = asyncio.Event()

    async def handler(j: Job) -> None:
        await asyncio.sleep(0.5)
        done.set()

    q = BeatScriptQueue([PersistenceError("EndpointConnectionError")])
    await worker(q, {"k.one": handler}, beat=0.05).run_once()  # must not raise
    assert done.is_set() and q.beats >= 5 and len(q.completed) == 1 and q.failed == []


async def test_persistent_heartbeat_failure_gives_up_before_the_lease_expires() -> None:
    cancelled = asyncio.Event()

    async def slow(j: Job) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    q = BeatScriptQueue([PersistenceError("ReadTimeoutError")])
    w = Worker(q, {"k.one": slow}, lease_seconds=1, heartbeat_seconds=0.1)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await w.run_once()  # must not raise
    elapsed = loop.time() - started
    assert cancelled.is_set() and elapsed < 1.0  # stopped before the 1 s lease ran out
    assert q.completed == [] and q.failed == []  # the lease may belong to another worker now


async def test_a_lost_lease_records_nothing_even_when_the_handler_swallows_cancel() -> None:
    async def stubborn(j: Job) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return  # returns normally after the cancel

    q = MemoryQueue([job()], keep_lease=False)
    await worker(q, {"k.one": stubborn}, beat=0.01).run_once()
    assert q.completed == [] and q.failed == []


async def test_a_lost_lease_records_no_failure_from_the_handler() -> None:
    async def fails_on_cancel(j: Job) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            raise JobFailedError("too_late") from None

    q = MemoryQueue([job()], keep_lease=False)
    await worker(q, {"k.one": fails_on_cancel}, beat=0.01).run_once()
    assert q.completed == [] and q.failed == []


class HangingBeatQueue(MemoryQueue):
    """A heartbeat that never answers (a hung DynamoDB call: read timeout x retries)."""

    def __init__(self) -> None:
        super().__init__([job()])
        self.cancelled_beats = 0

    async def heartbeat(self, job_id: uuid.UUID, owner: str, lease_seconds: int) -> bool:
        self.beats += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled_beats += 1
            raise
        return True  # pragma: no cover - never reached


async def test_a_successful_heartbeat_renews_the_lease_and_the_job_completes() -> None:
    async def medium(j: Job) -> None:
        await asyncio.sleep(0.15)

    q = MemoryQueue([job()])
    w = Worker(q, {"k.one": medium}, lease_seconds=1, heartbeat_seconds=0.05)
    await asyncio.wait_for(w.run_once(), 5)
    assert q.beats >= 2 and len(q.completed) == 1 and q.failed == []


async def test_a_hung_heartbeat_times_out_before_the_lease_runs_out_and_records_nothing() -> None:
    cancelled = asyncio.Event()

    async def slow(j: Job) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    q = HangingBeatQueue()
    w = Worker(q, {"k.one": slow}, lease_seconds=1, heartbeat_seconds=0.2)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(w.run_once(), 5)  # must not wait for the hung call
    elapsed = loop.time() - started
    assert cancelled.is_set() and q.cancelled_beats == 1
    assert elapsed < 1.0  # stopped before the 1 s lease ran out (minus the safety margin)
    assert q.completed == [] and q.failed == []  # the lease may belong to another worker now


async def test_a_timeout_raised_by_the_heartbeat_call_itself_is_a_transient_failure() -> None:
    """Only the worker's own renewal deadline means a lost lease: a TimeoutError from inside the
    call (a driver timeout) is retried on the next beat like any other failure."""

    async def medium(j: Job) -> None:
        await asyncio.sleep(0.2)

    q = BeatScriptQueue([TimeoutError("driver read timeout"), True])
    await asyncio.wait_for(worker(q, {"k.one": medium}, beat=0.02).run_once(), 5)
    assert q.beats >= 3 and len(q.completed) == 1 and q.failed == []


async def test_inner_timeouts_that_persist_give_up_before_the_lease_expires() -> None:
    cancelled = asyncio.Event()

    async def slow(j: Job) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    q = BeatScriptQueue([TimeoutError("driver read timeout")])
    w = Worker(q, {"k.one": slow}, lease_seconds=1, heartbeat_seconds=0.1)
    await asyncio.wait_for(w.run_once(), 5)
    assert cancelled.is_set() and q.beats >= 5  # retried as transient, not lost at the first
    assert q.completed == [] and q.failed == []


async def test_a_handler_that_finishes_after_a_heartbeat_timeout_is_not_completed() -> None:
    async def stubborn(j: Job) -> None:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            return  # finishes normally after the lease was given up

    q = HangingBeatQueue()
    w = Worker(q, {"k.one": stubborn}, lease_seconds=1, heartbeat_seconds=0.2)
    await asyncio.wait_for(w.run_once(), 5)
    assert q.completed == [] and q.failed == []  # no complete, no state overwrite


async def test_a_hung_heartbeat_does_not_delay_a_job_that_finishes_in_time() -> None:
    """Shutdown path: the handler finishes while a renewal hangs; stopping the heartbeat cancels
    the hung call at once and the job is completed (the last renewal still covers it)."""

    async def quick(j: Job) -> None:
        await asyncio.sleep(0.1)

    q = HangingBeatQueue()
    w = Worker(q, {"k.one": quick}, lease_seconds=60, heartbeat_seconds=0.02)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(w.run_once(), 5)
    assert loop.time() - started < 1.0 and q.cancelled_beats == 1
    assert len(q.completed) == 1 and q.failed == []


async def test_worker_shutdown_with_a_hung_heartbeat_stops_cleanly() -> None:
    finished = asyncio.Event()

    async def quick(j: Job) -> None:
        await asyncio.sleep(0.1)
        finished.set()

    q = HangingBeatQueue()
    w = Worker(q, {"k.one": quick}, lease_seconds=60, heartbeat_seconds=0.02, poll_seconds=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(w.run(stop))
    await asyncio.wait_for(finished.wait(), 5)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert len(q.completed) == 1 and q.failed == [] and q.cancelled_beats == 1


async def _ok(j: Job) -> None:
    return None


async def _dead(j: Job) -> None:
    raise JobFailedError("repository_unknown")


async def _retry(j: Job) -> None:
    raise JobRetryableError("jira_unavailable")


async def _slow(j: Job) -> None:
    await asyncio.sleep(30)


@pytest.mark.parametrize(
    ("handler", "queue"),
    [(_ok, lambda: MemoryQueue([job()])),
     (_dead, lambda: MemoryQueue([job()])),
     (_retry, lambda: MemoryQueue([job()])),
     (_slow, lambda: MemoryQueue([job()], keep_lease=False)),  # lease lost
     (_slow, HangingBeatQueue)],  # heartbeat timeout
    ids=["completed", "dead", "retried", "lease-lost", "heartbeat-timeout"],
)  # fmt: skip
async def test_the_claim_is_released_whatever_the_outcome(handler: Any, queue: Any) -> None:
    q = queue()
    (claimed,) = q.jobs
    w = Worker(q, {"k.one": handler}, lease_seconds=1, heartbeat_seconds=0.05)
    await asyncio.wait_for(w.run_once(), 5)
    assert q.released == [claimed.id]  # no per-job state is left in the queue adapter
