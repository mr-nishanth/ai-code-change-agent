"""A worker process that claims one queued job and is then SIGKILLed while holding its lease.

Run as: python -m tests.support.job_crash_worker <database_url> <owner> <lease_seconds>
Prints the claimed job id, then dies without releasing anything (no finally blocks run).
"""

import asyncio
import os
import signal
import sys
import time

from ai_code_change_agent.persistence.repositories.job_queue import PostgresJobQueue
from ai_code_change_agent.persistence.session import create_engine, create_session_factory


async def main(url: str, owner: str, lease: int) -> None:
    queue = PostgresJobQueue(create_session_factory(create_engine(url)))
    job = await queue.claim(owner, lease)
    print(job.id if job else "none", flush=True)
    if job is not None:
        await queue.heartbeat(job.id, owner, lease)  # mid-execution: lease is live
    os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(60)  # noqa: ASYNC251 - block: never run past the crash point


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
