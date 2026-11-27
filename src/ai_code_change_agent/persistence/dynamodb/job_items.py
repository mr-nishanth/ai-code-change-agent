"""Pure builders for a new job: `JOBKEY#` guard and `JOB#` item (DYNAMODB-DATA-MODEL.md 4, 8).

Both go into the same `TransactWriteItems` as the domain write that creates the job (W6 for webhook
intake, J1 for `JobQueue.enqueue`), each with `attribute_not_exists(PK)`:

- `JOBKEY#<dedupe_key>/GUARD` -> `job_id`: permanent (no TTL): one job per dedupe key, ever.
- `JOB#<jid>/JOB`: the job state (QUEUED, `attempts` 0) and, while it carries the sparse GSI3 keys
  `DISPATCH` / `<available_at>#<jid>`, its transactional-outbox entry. A worker-side producer sends
  `{"job_id"}` after commit and removes the GSI3 keys; the relay sends whatever is still there. The
  API never sends. The payload is stored as a compact JSON string (`payload_json`).

No I/O here: the webhook repository (Task 6) and the job queue (Task 12) share these builders."""

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.codec import encode_json, encode_timestamp
from ai_code_change_agent.persistence.dynamodb.errors import Item
from ai_code_change_agent.persistence.dynamodb.transactions import VERSION

JOB_STATUS_QUEUED = "QUEUED"
MAX_DEDUPE_KEY_LENGTH = 255
_KIND = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")


@dataclass(frozen=True)
class NewJob:
    """The two items that create one job, in the order they go into a transaction."""

    job_id: uuid.UUID
    guard: Item
    job: Item


def new_job(
    kind: str,
    payload: Mapping[str, Any],
    *,
    dedupe_key: str,
    max_attempts: int,
    now: datetime,
    available_at: datetime | None = None,
    job_id: uuid.UUID | None = None,
) -> NewJob:
    """Build the guard and job items for a QUEUED job that is due at `available_at` (default now).

    The constraints of the former `jobs` table are checked here, before any write: `kind`
    format, `max_attempts` 1..10 and a non-empty `dedupe_key` of at most 255 characters."""
    if not _KIND.match(kind):
        raise ValueError("job kind must match ^[a-z][a-z0-9_.]{2,63}$")
    if isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be 1..10")
    if not dedupe_key or len(dedupe_key) > MAX_DEDUPE_KEY_LENGTH:
        raise ValueError("dedupe_key must be 1..255 characters")
    jid = job_id or uuid.uuid4()
    due = available_at or now
    created = encode_timestamp(now)
    guard: Item = {
        **keys.job_dedupe_guard(dedupe_key).as_dict(),
        "entity": "job_dedupe_guard",
        "job_id": str(jid),
        "dedupe_key": dedupe_key,
        "created_at": created,
    }
    job: Item = {
        **keys.job(jid).as_dict(),
        **keys.job_dispatch_gsi3(due, jid),
        "entity": "job",
        "id": str(jid),
        "kind": kind,
        "payload_json": encode_json(dict(payload)),
        "dedupe_key": dedupe_key,
        "status": JOB_STATUS_QUEUED,
        "attempts": 0,
        "max_attempts": max_attempts,
        "available_at": encode_timestamp(due),
        VERSION: 1,
        "created_at": created,
        "updated_at": created,
    }
    return NewJob(jid, guard, job)


__all__ = ["JOB_STATUS_QUEUED", "MAX_DEDUPE_KEY_LENGTH", "NewJob", "new_job"]
