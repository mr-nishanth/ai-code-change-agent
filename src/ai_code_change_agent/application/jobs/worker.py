"""The worker: claims jobs, keeps their lease alive while a handler runs, and records results.

It is a trusted control-plane process (it controls the Docker daemon for sandboxes). Handlers are
registered by job kind; there is no default handler. A lost lease cancels the running handler so
two workers never act on the same job.
"""

import asyncio
import contextlib
import math
import socket
import uuid
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from ai_code_change_agent.application.jobs.queue import (
    Job,
    JobFailedError,
    JobQueue,
    JobRetryableError,
)
from ai_code_change_agent.core.logging import get_logger

Handler = Callable[[Job], Coroutine[Any, Any, None]]
_log = get_logger("worker")
MAX_BACKOFF_SECONDS = 900


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


class _LeaseLost(Exception):  # noqa: N818
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
        owner: str | None = None,
    ) -> None:
        self._queue = queue
        self._handlers = dict(handlers)
        self._lease = lease_seconds
        self._beat = heartbeat_seconds
        self._slots = asyncio.Semaphore(concurrency)
        self._poll = poll_seconds
        self.owner = owner or f"{socket.gethostname()[:64]}:{uuid.uuid4().hex[:12]}"

    async def run(self, stop: asyncio.Event) -> None:
        tasks: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                await self._slots.acquire()
                job = await self._queue.claim(self.owner, self._lease)
                if job is None:
                    self._slots.release()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), self._poll)
                    continue
                task = asyncio.create_task(self._guarded(job))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _guarded(self, job: Job) -> None:
        try:
            await self.process(job)
        finally:
            self._slots.release()

    async def run_once(self) -> Job | None:
        """Claim and process at most one job (tests and the CLI's one-shot mode)."""
        job = await self._queue.claim(self.owner, self._lease)
        if job is not None:
            await self.process(job)
        return job

    async def process(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            await self._queue.fail(job.id, self.owner, "no_handler", retry_in_seconds=None)
            _log.error("job_dead", job_id=str(job.id), kind=job.kind, reason="no_handler")
            return
        work = asyncio.create_task(handler(job))
        beat = asyncio.create_task(self._heartbeat(job, work))
        try:
            await work
        except _LeaseLost:
            _log.warning("job_lease_lost", job_id=str(job.id), kind=job.kind)
            return
        except asyncio.CancelledError:
            if beat.done() and isinstance(beat.exception(), _LeaseLost):
                _log.warning("job_lease_lost", job_id=str(job.id), kind=job.kind)
                return
            raise
        except JobFailedError as exc:
            await self._queue.fail(job.id, self.owner, exc.code, retry_in_seconds=None)
            _log.error("job_dead", job_id=str(job.id), kind=job.kind, reason=exc.code)
            return
        except Exception as exc:  # retryable by default; the code never carries raw messages
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
            return
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, _LeaseLost):
                await beat
        if await self._queue.complete(job.id, self.owner):
            _log.info("job_succeeded", job_id=str(job.id), kind=job.kind)

    async def _heartbeat(self, job: Job, work: asyncio.Task[None]) -> None:
        while True:
            await asyncio.sleep(self._beat)
            if not await self._queue.heartbeat(job.id, self.owner, self._lease):
                work.cancel()
                raise _LeaseLost
