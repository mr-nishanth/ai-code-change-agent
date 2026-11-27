"""DynamoDB + SQS on Floci: worker crashes never lose a job or run it twice concurrently.

Ports the former PostgreSQL test `test_worker_crash_db.py` (a real SIGKILL while a child job is leased, via
`tests/support/job_crash_worker.py`), then 100 randomised kill/restart iterations with fast
settings: a "kill" cancels the whole worker (its claim loops, handlers and heartbeats) and throws
away its queue object with the receipt handles, as a dead process would; nothing is released.
(`tests/integration/test_crash_recovery.py` covers dependency bundles on disk, not the database or
the queue, so it needs no DynamoDB port and runs unchanged.)"""

import asyncio
import random
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from ai_code_change_agent.application.jobs.queue import Job
from ai_code_change_agent.application.jobs.worker import Worker
from ai_code_change_agent.application.multirepo.coordinator import CHILD_JOB
from ai_code_change_agent.persistence.dynamodb import Table, keys
from ai_code_change_agent.persistence.dynamodb.job_queue import EXHAUSTED, DynamoSqsJobQueue
from tests.support.dynamo import JobQueues

pytestmark = [pytest.mark.floci, pytest.mark.integration]
REPO = Path(__file__).resolve().parents[3]


def make(table: Table, queues: JobQueues, **kwargs: Any) -> DynamoSqsJobQueue:
    kwargs.setdefault("receive_wait_seconds", 1)
    return DynamoSqsJobQueue(table, queues.sqs, queues.url, **kwargs)


async def test_a_sigkilled_worker_job_is_reclaimed_once_after_its_lease(
    app_table: Table, job_queues: JobQueues, floci: str
) -> None:
    queue = make(app_table, job_queues)
    child_id = uuid.uuid4()
    key = f"{CHILD_JOB}:{child_id}:0"
    assert await queue.enqueue(
        CHILD_JOB, {"child_id": str(child_id)}, dedupe_key=key, max_attempts=3
    )

    argv = [sys.executable, "-m", "tests.support.job_crash_worker", "dynamo", floci,
            app_table.name, job_queues.url, "worker-A", "3"]  # fmt: skip
    crashed = await asyncio.to_thread(
        subprocess.run, argv, cwd=REPO, capture_output=True, text=True, timeout=60, check=False
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr[-2000:]
    job_id = uuid.UUID(crashed.stdout.split()[0])

    assert await queue.claim("worker-B", 30) is None  # lease still held by the dead worker
    # the coordinator re-advancing after a restart cannot enqueue the same child run again
    assert not await queue.enqueue(
        CHILD_JOB, {"child_id": str(child_id)}, dedupe_key=key, max_attempts=3
    )

    await asyncio.sleep(3.5)  # lease (and the message's visibility) expires
    reclaimed = await queue.claim("worker-B", 30)
    assert reclaimed is not None and reclaimed.id == job_id and reclaimed.attempts == 2
    assert await queue.claim("worker-C", 30) is None  # exactly one live owner
    assert not await queue.heartbeat(job_id, "worker-A", 30)  # the dead owner cannot resume
    assert await queue.complete(job_id, "worker-B")

    stored = await app_table.get(keys.job(job_id))
    assert stored is not None and (stored["status"], stored["attempts"]) == ("SUCCEEDED", 2)


class Tracker:
    """Records handler runs; fails the test if one job ever runs twice at the same time."""

    def __init__(self) -> None:
        self.active: set[uuid.UUID] = set()
        self.overlaps: list[uuid.UUID] = []
        self.finished: set[uuid.UUID] = set()
        self.runs = 0

    async def handle(self, job: Job) -> None:
        if job.id in self.active:
            self.overlaps.append(job.id)
        self.active.add(job.id)
        self.runs += 1
        try:
            await asyncio.sleep(random.uniform(0.02, 0.2))  # noqa: S311 - test timing only
            self.finished.add(job.id)
        finally:
            self.active.discard(job.id)


async def test_randomised_kill_and_restart_loses_no_job_and_never_runs_one_twice_at_once(
    app_table: Table, job_queues: JobQueues
) -> None:
    rng = random.Random(2116)  # noqa: S311 - reproducible test schedule
    jobs = 40
    producer = make(app_table, job_queues)
    for i in range(jobs):
        assert await producer.enqueue("k.test", {"i": i}, dedupe_key=f"chaos-{i}", max_attempts=10)
    tracker = Tracker()

    def worker() -> Worker:  # a fresh process: new owner, new queue object, no receipts
        queue = make(app_table, job_queues, sweep_grace_seconds=2)
        return Worker(queue, {"k.test": tracker.handle}, lease_seconds=2,
                      heartbeat_seconds=0.5, concurrency=2, poll_seconds=0.05,
                      relay_seconds=0.05, sweep_seconds=1)  # fmt: skip

    for _ in range(100):
        task = asyncio.create_task(worker().run(asyncio.Event()))
        await asyncio.sleep(rng.uniform(0.05, 0.5))
        task.cancel()  # the kill: nothing is completed, failed or released
        await asyncio.gather(task, return_exceptions=True)
        assert not tracker.active and not tracker.overlaps

    stop = asyncio.Event()
    survivor = asyncio.create_task(worker().run(stop))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 90
    try:
        while True:
            done = [await app_table.get(keys.job(await _job_id(app_table, f"chaos-{i}")))
                    for i in range(jobs)]  # fmt: skip
            if all(d is not None and d["status"] in {"SUCCEEDED", "DEAD"} for d in done):
                break
            assert loop.time() < deadline, sorted(str(d and d["status"]) for d in done)
            await asyncio.sleep(0.5)
    finally:
        stop.set()
        await asyncio.wait_for(survivor, 30)
    assert tracker.overlaps == []
    # No job is lost: each one succeeded, or (killed in all 10 of its attempts) is DEAD with the
    # exhausted-lease code, never stuck or silently gone.
    for d in done:
        assert d is not None
        if d["status"] == "SUCCEEDED":
            assert uuid.UUID(d["id"]) in tracker.finished
        else:
            assert (d["last_error"], int(d["attempts"])) == (EXHAUSTED, 10)
    assert tracker.runs > jobs  # kills did interrupt running jobs, which were then reclaimed


async def _job_id(table: Table, dedupe_key: str) -> uuid.UUID:
    guard = await table.get(keys.job_dedupe_guard(dedupe_key))
    assert guard is not None
    return uuid.UUID(guard["job_id"])
