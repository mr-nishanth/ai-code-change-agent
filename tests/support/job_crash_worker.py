"""A worker process that claims one queued job and is then SIGKILLed while holding its lease.

Run as (DynamoDB + SQS on the local Floci emulator, explicit loopback endpoint, dummy credentials):
    python -m tests.support.job_crash_worker dynamo <endpoint> <table> <queue_url> <owner> <lease>
Prints the claimed job id, then dies without releasing anything (no finally blocks run).
"""

import asyncio
import os
import signal
import sys
import time
from typing import Any


def _dynamo(endpoint: str, table: str, queue_url: str) -> Any:
    from ai_code_change_agent.persistence.dynamodb import Table
    from ai_code_change_agent.persistence.dynamodb.job_queue import DynamoSqsJobQueue
    from tests.support.dynamo import local_clients
    from tests.support.floci import require_loopback

    aws = local_clients(require_loopback(endpoint))
    return DynamoSqsJobQueue(Table(aws.dynamodb, table), aws.sqs, queue_url, receive_wait_seconds=5)


async def main(queue: Any, owner: str, lease: int) -> None:
    job = await queue.claim(owner, lease)
    print(job.id if job else "none", flush=True)
    if job is not None:
        await queue.heartbeat(job.id, owner, lease)  # mid-execution: lease is live
    os.kill(os.getpid(), signal.SIGKILL)
    time.sleep(60)  # noqa: ASYNC251 - block: never run past the crash point


if __name__ == "__main__":
    _, _, endpoint, table, queue_url, owner, lease = sys.argv  # argv[1] == "dynamo"
    asyncio.run(main(_dynamo(endpoint, table, queue_url), owner, int(lease)))
