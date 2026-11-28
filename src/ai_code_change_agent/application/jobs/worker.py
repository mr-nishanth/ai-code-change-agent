"""The worker: claims jobs, keeps their lease alive while a handler runs, and records results.

It is a trusted control-plane process (it controls the Docker daemon for sandboxes). Handlers are
registered by job kind; there is no default handler. A lost lease cancels the running handler so
two workers never act on the same job.

`run` starts one claim loop per concurrency slot (with SQS each claim is one long-poll receive) and
the dispatcher role: the queue's outbox relay (`dispatch_pending`) every `relay_seconds` and its
sweep for lost messages every `sweep_seconds`, inside this process (DYNAMODB-DATA-MODEL.md 8.2).
The relay is only a fallback (producers send right after commit), so both intervals are long by
default (30 s and 300 s, `JOB_RELAY_SECONDS` / `JOB_SWEEP_SECONDS`): each tick costs a GSI3 Query.
A failing claim is logged and retried after `poll_seconds`, a failing dispatcher tick at the next
relay tick; neither stops the worker.

Every job that ends DEAD runs its kind's `on_dead` hook, whichever path recorded it: this worker's
own last failure runs it at once; a job a claim found exhausted with an expired lease (a crashed
worker's last attempt) is listed by `JobQueue.dead_pending` and run by the dispatcher on its next
relay tick. `periodic` steps (the Jira report retry) run on the same tick.

Shutdown (`stop` set, SIGINT/SIGTERM in the CLI): no new claim starts; a claim already in its
long poll (at most `receive_wait_seconds`, 20 s) that returns a job gives it straight back
(`release_lease(refund=True)`). A running handler gets `shutdown_grace_seconds` to finish; then
(or at once when `force` is set, a second signal) it is cancelled and its lease released
(`release_lease`), so another worker or a restart resumes it right away instead of waiting for
the lease to run out.
"""

import asyncio
import contextlib
import math
import socket
import uuid
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ai_code_change_agent.application.jobs.queue import (
    Job,
    JobFailedError,
    JobQueue,
    JobRetryableError,
    JobStatus,
)
from ai_code_change_agent.core.logging import get_logger

Handler = Callable[[Job], Coroutine[Any, Any, None]]
Periodic = tuple[str, Callable[[], Coroutine[Any, Any, int]]]
_log = get_logger("worker")
MAX_BACKOFF_SECONDS = 900
# A renewal must answer this long before the last successful one runs out (clock skew between
# this host and DynamoDB, plus the cancel itself): at most 5 s, a tenth of shorter leases.
HEARTBEAT_MARGIN_SECONDS = 5.0


def backoff_seconds(attempts: int, base: int = 30) -> int:
    exponent: int = max(attempts - 1, 0)
    return int(min(base * (1 << exponent), MAX_BACKOFF_SECONDS))


def retry_delay_seconds(exc: BaseException, attempts: int) -> int:
    """The backoff for a retryable failure, raised to the exception's positive
    `retry_after_seconds` hint (rounded up) and capped at MAX_BACKOFF_SECONDS."""
    delay = backoff_seconds(attempts)
    hint = getattr(exc, "retry_after_seconds", None)
    if isinstance(hint, int | float) and math.isfinite(hint) and hint > 0:
        delay = max(delay, math.ceil(hint))
    return min(delay, MAX_BACKOFF_SECONDS)


def _code(exc: BaseException) -> str:
    """A log-safe code: the error's string `code` attribute, else its class name; never raw text."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else type(exc).__name__


class _LeaseLost(Exception):  # noqa: N818
    pass


class _ShutdownCancelled(Exception):  # noqa: N818 - the shutdown grace ran out
    pass


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        lease_seconds: int,
        heartbeat_seconds: float,
        concurrency: int = 1,
        poll_seconds: float = 2.0,
        relay_seconds: float = 30.0,
        sweep_seconds: float = 300.0,
        owner: str | None = None,
        on_dead: Mapping[str, Handler] | None = None,
        periodic: Sequence[Periodic] = (),
        shutdown_grace_seconds: float = 30.0,
    ) -> None:
        self._queue = queue
        self._handlers = dict(handlers)
        # per kind, run once for every job that ends DEAD (a refusal, the last attempt, or a
        # crashed worker's last attempt found by a claim), to end what the job leaves behind
        self._on_dead = dict(on_dead or {})
        self._periodic = tuple(periodic)
        self._grace = shutdown_grace_seconds
        self._force: asyncio.Event | None = None
        self._lease = lease_seconds
        self._beat = heartbeat_seconds
        self._concurrency = concurrency
        self._poll = poll_seconds
        self._relay = relay_seconds
        self._sweep = sweep_seconds
        self.owner = owner or f"{socket.gethostname()[:64]}:{uuid.uuid4().hex[:12]}"

    async def run(self, stop: asyncio.Event, force: asyncio.Event | None = None) -> None:
        self._force = force
        slots = [asyncio.create_task(self._slot(stop)) for _ in range(self._concurrency)]
        dispatcher = asyncio.create_task(self._dispatcher(stop))
        try:
            await asyncio.gather(*slots)
        finally:
            for slot in slots:
                slot.cancel()
            dispatcher.cancel()
            await asyncio.gather(*slots, dispatcher, return_exceptions=True)

    async def _slot(self, stop: asyncio.Event) -> None:
        """One free slot: claim (a long-poll receive with SQS), process, repeat until stopped. A
        job already claimed is processed to the end even when stop is set meanwhile."""
        while not stop.is_set():
            try:
                job = await self._queue.claim(self.owner, self._lease)
            except Exception as exc:  # a store outage must not stop the worker
                _log.warning("job_claim_failed", code=_code(exc))
                job = None
            if job is None:
                await self._wait(stop, self._poll)
                continue
            if stop.is_set():  # claimed during shutdown: give it back untouched
                try:
                    await self._queue.release_lease(job.id, self.owner, refund=True)
                except Exception as exc:  # the lease runs out and decides
                    _log.warning("job_release_failed", job_id=str(job.id), code=_code(exc))
                finally:
                    self._queue.release(job.id, self.owner)
                break
            try:
                await self.process(job, stop)
            except Exception as exc:  # recording the result failed; the lease expires and decides
                _log.warning("job_process_failed", job_id=str(job.id), code=_code(exc))

    async def _dispatcher(self, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        next_sweep = loop.time()
        while not stop.is_set():
            await self._tick("dispatch_pending", self._queue.dispatch_pending)
            await self._tick("dead_hooks", self._dead_hooks)
            for name, step in self._periodic:
                await self._tick(name, step)
            if loop.time() >= next_sweep:
                await self._tick("sweep", self._queue.sweep)
                next_sweep = loop.time() + self._sweep
            await self._wait(stop, self._relay)

    @staticmethod
    async def _tick(name: str, step: Callable[[], Coroutine[Any, Any, int]]) -> None:
        try:
            sent = await step()
        except Exception as exc:
            _log.warning("job_dispatcher_failed", step=name, code=_code(exc))
            return
        if sent:
            _log.info("job_dispatcher_sent", step=name, count=sent)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def run_once(self) -> Job | None:
        """Claim and process at most one job (tests and the CLI's one-shot mode)."""
        job = await self._queue.claim(self.owner, self._lease)
        if job is not None:
            await self.process(job)
        return job

    async def _dead_hooks(self) -> int:
        """Run the on_dead hook of every DEAD job the queue lists (recorded DEAD by any path)."""
        jobs = await self._queue.dead_pending()
        for job in jobs:
            _log.warning("job_dead_recovered", job_id=str(job.id), kind=job.kind,
                         reason=job.last_error)  # fmt: skip
            await self._dead(job)
        return len(jobs)

    async def process(self, job: Job, stop: asyncio.Event | None = None) -> None:
        # identifiers only: the payload can hold Story text and is never logged
        _log.info(
            "job_claimed",
            job_id=str(job.id),
            kind=job.kind,
            attempt=job.attempts,
            max_attempts=job.max_attempts,
        )
        handler = self._handlers.get(job.kind)
        if handler is None:
            status = await self._queue.fail(job.id, self.owner, "no_handler", retry_in_seconds=None)
            _log.error("job_dead", job_id=str(job.id), kind=job.kind, reason="no_handler")
            if status is JobStatus.DEAD:
                await self._dead(replace(job, last_error="no_handler"))
            return
        try:
            await self._run(job, handler, stop)
        finally:  # whatever happened, the job is out of this worker's hands
            self._queue.release(job.id, self.owner)

    async def _dead(self, job: Job) -> None:
        """Run the kind's hook (idempotent) and record that it ran; a hook that raises is logged
        and listed again by `dead_pending` later (bounded), never changing the DEAD status."""
        hook = self._on_dead.get(job.kind)
        failed = False
        if hook is not None:
            try:
                await hook(job)
            except Exception as exc:  # the job is already DEAD; the hook never changes that
                failed = True
                _log.warning(
                    "job_dead_hook_failed", job_id=str(job.id), kind=job.kind, code=_code(exc)
                )
        try:
            await self._queue.dead_settled(job.id, failed=failed)
        except Exception as exc:  # listed again by dead_pending: the hook runs once more
            _log.warning("job_dead_settle_failed", job_id=str(job.id), code=_code(exc))

    async def _shutdown(self, stop: asyncio.Event, job: Job, work: asyncio.Task[None]) -> None:
        """After `stop`, give the running handler `shutdown_grace_seconds` (less when `force` is
        set), then cancel it; `_run` releases the lease."""
        await stop.wait()
        waiters: list[asyncio.Task[Any]] = [asyncio.create_task(asyncio.sleep(self._grace))]
        if self._force is not None:
            waiters.append(asyncio.create_task(self._force.wait()))
        try:
            await asyncio.wait([work, *waiters], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
        if not work.done():
            _log.warning("job_shutdown_cancelled", job_id=str(job.id), kind=job.kind)
            work.cancel()
            raise _ShutdownCancelled

    async def _run(self, job: Job, handler: Handler, stop: asyncio.Event | None = None) -> None:
        work = asyncio.create_task(handler(job))
        beat = asyncio.create_task(self._heartbeat(job, work))
        watch = asyncio.create_task(self._shutdown(stop, job, work)) if stop is not None else None
        try:
            await work
        except _LeaseLost:
            self._lease_lost(job)
            return
        except asyncio.CancelledError:
            if _lost(beat):
                self._lease_lost(job)
                return
            if watch is not None and _cancelled_by(watch):
                await self._stop_beat(beat)
                await self._queue.release_lease(job.id, self.owner, refund=False)
                return
            raise
        except JobFailedError as exc:
            if await self._stop_beat(beat):
                self._lease_lost(job)
                return
            status = await self._queue.fail(job.id, self.owner, exc.code, retry_in_seconds=None)
            _log.error("job_dead", job_id=str(job.id), kind=job.kind, reason=exc.code)
            if status is JobStatus.DEAD:  # not when the store refused (another owner has it)
                await self._dead(replace(job, last_error=exc.code))
            return
        except Exception as exc:  # retryable by default; the code never carries raw messages
            if await self._stop_beat(beat):
                self._lease_lost(job)
                return
            code = exc.code if isinstance(exc, JobRetryableError) else type(exc).__name__
            status = await self._queue.fail(
                job.id, self.owner, code, retry_in_seconds=retry_delay_seconds(exc, job.attempts)
            )
            _log.warning(
                "job_failed",
                job_id=str(job.id),
                kind=job.kind,
                code=code,
                status=status.value,
                attempts=job.attempts,
            )
            if status is JobStatus.DEAD:
                _log.error("job_dead", job_id=str(job.id), kind=job.kind, reason=code)
                await self._dead(replace(job, last_error=code))
            return
        finally:
            if watch is not None and not watch.done():
                watch.cancel()
            await self._stop_beat(beat)
        if _lost(beat):  # the lease went while the handler was finishing: not ours to record
            self._lease_lost(job)
            return
        if await self._queue.complete(job.id, self.owner):
            _log.info("job_succeeded", job_id=str(job.id), kind=job.kind)

    @staticmethod
    def _lease_lost(job: Job) -> None:
        _log.warning("job_lease_lost", job_id=str(job.id), kind=job.kind)

    @staticmethod
    async def _stop_beat(beat: asyncio.Task[None]) -> bool:
        """Stop the heartbeat task; nothing it raised escapes. True when it saw the lease lost."""
        beat.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await beat
        return _lost(beat)

    async def _heartbeat(self, job: Job, work: asyncio.Task[None]) -> None:
        """Renew the lease every `heartbeat_seconds` until cancelled.

        `False` from the queue means the lease is gone (another worker may own the job): the
        handler is cancelled and nothing is recorded for it. A failing renewal (a store outage, a
        timeout, exhausted throttling retries) is logged and retried on the next beat while the last
        successful renewal still covers it; when the next retry would land after that lease ran
        out, the worker treats the lease as lost too, so it stops before another worker can claim
        the job. Each renewal is bounded by its own timeout that ends the safety margin before the
        last successful renewal runs out: a call that hangs (driver read timeouts times retries)
        counts as a lost lease, so the handler never keeps running past `covered_until`. Only that
        deadline's own expiry is a lost lease; a `TimeoutError` raised by the call itself (a driver
        timeout) is a transient failure like any other. The abandoned call keeps running in its
        thread; the queue's renewal clients bound it to about one margin (`heartbeat_config`), and
        a late success only extends this owner's lease (the renewal is conditional on the owner),
        never lets a second worker run the job. Nothing raised here reaches the completion path
        (see `_stop_beat`)."""
        loop = asyncio.get_running_loop()
        covered_until = loop.time() + self._lease  # the claim just set the lease
        margin = min(HEARTBEAT_MARGIN_SECONDS, self._lease / 10)
        failures = 0
        while True:
            await asyncio.sleep(self._beat)
            asked_at = loop.time()  # a renewal's lease runs from the request, not the reply
            deadline = asyncio.timeout(max(0.0, covered_until - asked_at - margin))
            try:
                async with deadline:
                    renewed = await self._queue.heartbeat(job.id, self.owner, self._lease)
            except Exception as exc:
                if isinstance(exc, TimeoutError) and deadline.expired():
                    # our own deadline: no answer before the lease would lapse, treat it as lost
                    _log.warning("job_heartbeat_timeout", job_id=str(job.id), kind=job.kind)
                    renewed = False
                else:  # transient (a TimeoutError from inside the call too): retry while covered
                    failures += 1
                    _log.warning(
                        "job_heartbeat_failed",
                        job_id=str(job.id),
                        kind=job.kind,
                        code=_code(exc),
                        failures=failures,
                    )
                    if loop.time() + self._beat < covered_until:
                        continue
                    renewed = False
            else:
                if renewed:
                    covered_until = asked_at + self._lease
                    failures = 0
            if not renewed:
                work.cancel()
                raise _LeaseLost


def _lost(beat: asyncio.Task[None]) -> bool:
    return beat.done() and not beat.cancelled() and isinstance(beat.exception(), _LeaseLost)


def _cancelled_by(watch: asyncio.Task[None]) -> bool:
    return (
        watch.done() and not watch.cancelled() and isinstance(watch.exception(), _ShutdownCancelled)
    )
