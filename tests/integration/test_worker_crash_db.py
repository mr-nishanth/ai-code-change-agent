"""PostgreSQL + real SIGKILL: a worker killed while executing a child job neither loses the job
nor lets it run twice concurrently, and the coordinator cannot enqueue a duplicate child run."""

import asyncio
import signal
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_code_change_agent.application.multirepo.coordinator import CHILD_JOB
from ai_code_change_agent.persistence.repositories.job_queue import PostgresJobQueue
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from tests.integration.conftest import libpq

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
async def queue(database_url: str) -> AsyncIterator[PostgresJobQueue]:
    engine = create_engine(database_url)
    sessions: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    yield PostgresJobQueue(sessions)
    await engine.dispose()


async def test_a_sigkilled_worker_job_is_reclaimed_once_after_its_lease(
    queue: PostgresJobQueue, database_url: str
) -> None:
    child_id = uuid.uuid4()
    key = f"{CHILD_JOB}:{child_id}:0"
    assert await queue.enqueue(
        CHILD_JOB, {"child_id": str(child_id)}, dedupe_key=key, max_attempts=3
    )

    crashed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.support.job_crash_worker", database_url, "worker-A", "3"],
        cwd=REPO, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr[-2000:]
    job_id = uuid.UUID(crashed.stdout.split()[0])

    assert await queue.claim("worker-B", 30) is None  # lease still held by the dead worker
    # the coordinator re-advancing after a restart cannot enqueue the same child run again
    assert not await queue.enqueue(
        CHILD_JOB, {"child_id": str(child_id)}, dedupe_key=key, max_attempts=3
    )

    await asyncio.sleep(3.5)  # lease expires
    reclaimed = await queue.claim("worker-B", 30)
    assert reclaimed is not None and reclaimed.id == job_id and reclaimed.attempts == 2
    assert await queue.claim("worker-C", 30) is None  # exactly one live owner
    assert not await queue.heartbeat(job_id, "worker-A", 30)  # the dead owner cannot resume
    await queue.complete(job_id, "worker-B")

    with psycopg.connect(libpq(database_url), autocommit=True) as conn:
        rows = conn.execute(
            "SELECT status, attempts FROM jobs WHERE dedupe_key = %s", (key,)
        ).fetchall()
    assert rows == [("SUCCEEDED", 2)]
