"""DynamoDB job state + SQS delivery: the `JobQueue` adapter (DYNAMODB-DATA-MODEL.md 8, J1-J8).

DynamoDB is authoritative; SQS only says "look at job <id>". The job item (`JOB#<jid>/JOB`)
carries the state machine of the former `jobs` table (`QUEUED -> RUNNING -> SUCCEEDED | QUEUED |
DEAD`) and, through the sparse GSI3, the transactional outbox:

- `GSI3PK = DISPATCH`: the job's message was never sent (a fresh job; the API's intake jobs
  always start here, since the API never calls SQS). The relay (`dispatch_pending`) sends it.
- `GSI3PK = SWEEP`: the job is not terminal and its message was sent (`dispatched_at`). The sweep
  re-sends a message for such a job when it is overdue: QUEUED and due, or RUNNING with an expired
  lease, by more than the grace period, and not re-sent within it (a lost or dead-lettered message).
- no GSI3 keys: terminal (SUCCEEDED / DEAD, `expires_at` set for TTL).

Every message is `{"job_id": "<uuid>"}`; every receipt goes through the conditional claim, so a
duplicate, a relay or sweep re-send or a late redelivery is a no-op. A message that is not
claimable is handled per DM 8.2: terminal -> `DeleteMessage`; not due -> visibility until
`available_at`; leased elsewhere -> visibility = remaining lease; exhausted with an expired lease ->
DEAD (`lease_expired_attempts_exhausted`) and `DeleteMessage`. A body that is not a job message, or
names no job, is left alone so SQS moves it to the DLQ after `maxReceiveCount`.

The lease in DynamoDB decides ownership; SQS visibility follows it on a best-effort basis (a failed
`ChangeMessageVisibility` or `DeleteMessage` is logged, never fatal: the next receipt reconciles).
Receipt handles are kept in process (`job_id -> receipt`). Every write bumps `row_version`.
SQS and DynamoDB failures that matter surface as `PersistenceError` whose message is a code, never
driver text."""

import asyncio
import contextlib
import json
import math
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError  # type: ignore[import-untyped]

from ai_code_change_agent.application.jobs.queue import Job, JobStatus
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.clock import Clock, SystemClock
from ai_code_change_agent.persistence.dynamodb.codec import (
    decode_enum,
    decode_int,
    decode_json,
    decode_timestamp,
    decode_uuid,
    encode_epoch_seconds,
    encode_timestamp,
)
from ai_code_change_agent.persistence.dynamodb.errors import Item, PersistenceError
from ai_code_change_agent.persistence.dynamodb.job_items import new_job
from ai_code_change_agent.persistence.dynamodb.table import Table

_log = get_logger("job_queue")

MAX_VISIBILITY_SECONDS = 43_200  # SQS maximum; Floci does not enforce it (DM 10.4)
MAX_RECEIVE_WAIT_SECONDS = 20  # SQS long-poll maximum
RETENTION_DAYS = 90  # TTL on terminal jobs (DM 7)
RELAY_BATCH = 100
EXHAUSTED = "lease_expired_attempts_exhausted"
_TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.DEAD})
_NEW = "attribute_not_exists(PK)"
_BUMP = "row_version = row_version + :one, updated_at = :now"
_TERMINAL_SET = "finished_at = :now, expires_at = :ttl, " + _BUMP
_TERMINAL_REMOVE = "lease_owner, lease_expires_at, GSI3PK, GSI3SK"
_S = {"#s": "status"}


class _Duplicate(Exception):  # noqa: N818 - internal signal: the dedupe guard exists
    pass


class _Stale(Exception):  # noqa: N818 - internal signal: a condition failed; carries the old image
    def __init__(self, old: Item | None) -> None:
        super().__init__("stale")
        self.old = old


def _stale(old: Item | None) -> BaseException:
    return _Stale(old)


def _seconds_until(later: datetime, now: datetime) -> int:
    """Whole seconds from `now` to `later` (rounded up), within SQS's visibility range."""
    return max(0, min(MAX_VISIBILITY_SECONDS, math.ceil((later - now).total_seconds())))


def _job_id(body: object) -> uuid.UUID | None:
    if not isinstance(body, str):
        return None
    try:
        document = json.loads(body)
    except ValueError:
        return None
    if not isinstance(document, dict) or set(document) != {"job_id"}:
        return None
    try:
        return uuid.UUID(str(document["job_id"]))
    except ValueError:
        return None


class DynamoSqsJobQueue:
    """`JobQueue` over the application table and the SQS job queue (standard, with a DLQ).

    `queue_url` is the URL or a callable that returns it (`AwsClients.queue_url`, resolved on
    first use). `receive_wait_seconds` is the long-poll wait of one claim (0..20);
    `sweep_grace_seconds` is how long a job may be overdue before the sweep re-sends it."""

    def __init__(
        self,
        table: Table,
        sqs: Any,
        queue_url: str | Callable[[], str],
        *,
        clock: Clock | None = None,
        receive_wait_seconds: int = MAX_RECEIVE_WAIT_SECONDS,
        sweep_grace_seconds: int = 300,
        retention_days: int = RETENTION_DAYS,
    ) -> None:
        if not 0 <= receive_wait_seconds <= MAX_RECEIVE_WAIT_SECONDS:
            raise ValueError("receive_wait_seconds must be 0..20")
        if sweep_grace_seconds < 0:
            raise ValueError("sweep_grace_seconds must not be negative")
        self._table = table
        self._sqs = sqs
        self._queue_url = queue_url
        self._clock: Clock = clock or SystemClock()
        self._wait = receive_wait_seconds
        self._grace = timedelta(seconds=sweep_grace_seconds)
        self._retention = timedelta(days=retention_days)
        self._receipts: dict[uuid.UUID, tuple[str, str]] = {}  # job -> (owner, receipt)

    # SQS plumbing ------------------------------------------------------------------------------

    async def _url(self) -> str:
        if not isinstance(self._queue_url, str):
            resolver = self._queue_url
            try:
                self._queue_url = await asyncio.to_thread(resolver)
            except (ClientError, BotoCoreError) as exc:
                code = _code(exc)
            else:
                return self._queue_url
            raise PersistenceError(code)
        return self._queue_url

    async def _sqs_call(self, operation: str, **request: Any) -> dict[str, Any]:
        request["QueueUrl"] = await self._url()
        method = getattr(self._sqs, operation)
        try:
            response: dict[str, Any] = await asyncio.to_thread(method, **request)
        except (ClientError, BotoCoreError) as exc:
            code = _code(exc)
        else:
            return response
        raise PersistenceError(code)  # outside the except block: nothing from botocore is chained

    async def _best_effort(self, operation: str, job_id: uuid.UUID, **request: Any) -> bool:
        try:
            await self._sqs_call(operation, **request)
        except PersistenceError as exc:
            _log.warning(
                "job_sqs_call_failed", job_id=str(job_id), operation=operation, code=str(exc)
            )
            return False
        return True

    async def _send(self, job_id: uuid.UUID) -> bool:
        return await self._best_effort(
            "send_message", job_id, MessageBody=json.dumps({"job_id": str(job_id)})
        )

    async def _visibility(self, job_id: uuid.UUID, receipt: str | None, seconds: int) -> None:
        if receipt is not None:
            await self._best_effort(
                "change_message_visibility",
                job_id,
                ReceiptHandle=receipt,
                VisibilityTimeout=max(0, min(MAX_VISIBILITY_SECONDS, seconds)),
            )

    async def _delete(self, job_id: uuid.UUID, receipt: str | None) -> None:
        if receipt is not None:
            await self._best_effort("delete_message", job_id, ReceiptHandle=receipt)

    # Enqueue (J1) ------------------------------------------------------------------------------

    async def enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str, max_attempts: int
    ) -> bool:
        now = self._clock.now()
        job = new_job(kind, payload, dedupe_key=dedupe_key, max_attempts=max_attempts, now=now)
        tx = self._table.transaction()
        tx.put(job.guard, condition=_NEW, on_failed=_Duplicate())
        tx.put(job.job, condition=_NEW)
        try:
            await tx.commit()
        except _Duplicate:
            return False
        # Worker-side producer: send now; if this process dies first, the relay sends it.
        if await self._send(job.job_id):
            await self._mark_dispatched(job.job_id, keys.GSI_DISPATCH)
        return True

    async def _mark_dispatched(self, job_id: uuid.UUID, partition: str) -> None:
        """Move the job from the `partition` of GSI3 to `SWEEP` with `dispatched_at` = now; a
        no-op if the job left that partition meanwhile (claimed and finished, or swept)."""
        with contextlib.suppress(_Stale):
            await self._table.update(
                keys.job(job_id),
                update="SET GSI3PK = :sweep, dispatched_at = :now, " + _BUMP,
                condition="GSI3PK = :from",
                values={
                    ":sweep": keys.GSI_SWEEP,
                    ":from": partition,
                    ":now": encode_timestamp(self._clock.now()),
                    ":one": 1,
                },
                on_failed=_Stale(None),
                return_values="NONE",
            )

    # Claim (J2, J3) ----------------------------------------------------------------------------

    async def claim(self, owner: str, lease_seconds: int) -> Job | None:
        response = await self._sqs_call(
            "receive_message",
            MaxNumberOfMessages=1,
            WaitTimeSeconds=self._wait,
            VisibilityTimeout=max(0, min(MAX_VISIBILITY_SECONDS, lease_seconds)),
        )
        messages = response.get("Messages") or []
        if not messages:
            return None
        message = messages[0]
        receipt = message.get("ReceiptHandle")
        job_id = _job_id(message.get("Body"))
        if job_id is None or not isinstance(receipt, str):
            _log.warning("job_message_invalid")  # left for the DLQ (maxReceiveCount)
            return None
        return await self._claim(job_id, receipt, owner, lease_seconds)

    async def _claim(
        self, job_id: uuid.UUID, receipt: str, owner: str, lease_seconds: int
    ) -> Job | None:
        now = self._clock.now()
        try:
            item = await self._table.update(
                keys.job(job_id),
                update=(
                    "SET #s = :running, lease_owner = :owner, lease_expires_at = :lease, "
                    "heartbeat_at = :now, attempts = attempts + :one, " + _BUMP
                ),
                condition=(
                    "attempts < max_attempts AND ((#s = :queued AND available_at <= :now) "
                    "OR (#s = :running AND lease_expires_at < :now))"
                ),
                names=_S,
                values={
                    ":running": JobStatus.RUNNING.value,
                    ":queued": JobStatus.QUEUED.value,
                    ":owner": owner,
                    ":lease": encode_timestamp(now + timedelta(seconds=lease_seconds)),
                    ":now": encode_timestamp(now),
                    ":one": 1,
                },
                on_failed=_stale,
            )
        except _Stale as stale:
            await self._not_claimable(job_id, receipt, stale.old, now)
            return None
        self._receipts[job_id] = (owner, receipt)
        return Job(
            decode_uuid(item.get("id")),
            str(item.get("kind")),
            dict(decode_json(item.get("payload_json"), "payload_json")),
            decode_int(item.get("attempts"), "attempts"),
            decode_int(item.get("max_attempts"), "max_attempts"),
        )

    async def _not_claimable(
        self, job_id: uuid.UUID, receipt: str, old: Item | None, now: datetime
    ) -> None:
        if old is None:
            old = await self._table.get(keys.job(job_id))
        if old is None:
            _log.warning("job_message_unknown_job", job_id=str(job_id))  # left for the DLQ
            return
        status = decode_enum(JobStatus, old.get("status"), "status")
        if status in _TERMINAL:
            await self._delete(job_id, receipt)
            return
        exhausted = decode_int(old.get("attempts")) >= decode_int(old.get("max_attempts"))
        if status is JobStatus.QUEUED:
            due = decode_timestamp(old.get("available_at"), "available_at")
            if exhausted:  # not reachable through fail(); never leave it claimable-looking
                await self._mark_dead(job_id, old, "attempts_exhausted", receipt)
            else:
                await self._visibility(job_id, receipt, max(1, _seconds_until(due, now)))
            return
        lease = decode_timestamp(old.get("lease_expires_at"), "lease_expires_at")
        if lease >= now:  # leased elsewhere (or by us: then this is the newest receipt)
            held = self._receipts.get(job_id)
            if held is not None and held[0] == old.get("lease_owner"):
                self._receipts[job_id] = (held[0], receipt)
            await self._visibility(job_id, receipt, max(1, _seconds_until(lease, now)))
        elif exhausted:
            await self._mark_dead(job_id, old, EXHAUSTED, receipt)
        else:  # became claimable after the condition was evaluated: deliver again at once
            await self._visibility(job_id, receipt, 0)

    async def _mark_dead(self, job_id: uuid.UUID, old: Item, code: str, receipt: str) -> None:
        try:
            await self._table.update(
                keys.job(job_id),
                update=f"SET #s = :dead, last_error = :code, {_TERMINAL_SET} "
                f"REMOVE {_TERMINAL_REMOVE}",
                condition="row_version = :seen",
                names=_S,
                values={
                    ":dead": JobStatus.DEAD.value,
                    ":code": code,
                    ":seen": old.get("row_version"),
                    **self._terminal_values(),
                },
                on_failed=_Stale(None),
                return_values="NONE",
            )
        except _Stale:
            return  # changed meanwhile; the next receipt decides again
        self._receipts.pop(job_id, None)
        await self._delete(job_id, receipt)
        _log.warning("job_dead", job_id=str(job_id), reason=code)

    def _receipt(self, job_id: uuid.UUID, owner: str) -> str | None:
        """The newest receipt of the message through which `owner` claimed the job."""
        held = self._receipts.get(job_id)
        return held[1] if held is not None and held[0] == owner else None

    def _forget(self, job_id: uuid.UUID, owner: str) -> str | None:
        receipt = self._receipt(job_id, owner)
        if receipt is not None:
            del self._receipts[job_id]
        return receipt

    def _terminal_values(self) -> dict[str, Any]:
        now = self._clock.now()
        return {
            ":now": encode_timestamp(now),
            ":ttl": encode_epoch_seconds(now + self._retention),
            ":one": 1,
        }

    # Lease (J4), completion (J5), failure (J6) -------------------------------------------------

    async def heartbeat(self, job_id: uuid.UUID, owner: str, lease_seconds: int) -> bool:
        now = self._clock.now()
        try:
            await self._table.update(
                keys.job(job_id),
                update="SET lease_expires_at = :lease, heartbeat_at = :now, " + _BUMP,
                condition="#s = :running AND lease_owner = :owner AND lease_expires_at >= :now",
                names=_S,
                values={
                    ":running": JobStatus.RUNNING.value,
                    ":owner": owner,
                    ":lease": encode_timestamp(now + timedelta(seconds=lease_seconds)),
                    ":now": encode_timestamp(now),
                    ":one": 1,
                },
                on_failed=_Stale(None),
                return_values="NONE",
            )
        except _Stale:
            self._forget(job_id, owner)
            return False
        await self._visibility(job_id, self._receipt(job_id, owner), lease_seconds)
        return True

    async def complete(self, job_id: uuid.UUID, owner: str) -> bool:
        try:
            await self._table.update(
                keys.job(job_id),
                update=f"SET #s = :done, {_TERMINAL_SET} REMOVE last_error, {_TERMINAL_REMOVE}",
                condition="#s = :running AND lease_owner = :owner",
                names=_S,
                values={
                    ":done": JobStatus.SUCCEEDED.value,
                    ":running": JobStatus.RUNNING.value,
                    ":owner": owner,
                    **self._terminal_values(),
                },
                on_failed=_Stale(None),
                return_values="NONE",
            )
        except _Stale:
            return False
        await self._delete(job_id, self._forget(job_id, owner))
        return True

    async def fail(
        self, job_id: uuid.UUID, owner: str, code: str, *, retry_in_seconds: int | None
    ) -> JobStatus:
        owned = "#s = :running AND lease_owner = :owner"
        base = {":running": JobStatus.RUNNING.value, ":owner": owner, ":code": code[:200]}
        if retry_in_seconds is not None:
            now = self._clock.now()
            try:
                await self._table.update(
                    keys.job(job_id),
                    update="SET #s = :queued, available_at = :due, last_error = :code, "
                    + _BUMP
                    + " REMOVE lease_owner, lease_expires_at",
                    condition=f"{owned} AND attempts < max_attempts",
                    names=_S,
                    values={
                        **base,
                        ":queued": JobStatus.QUEUED.value,
                        ":due": encode_timestamp(now + timedelta(seconds=retry_in_seconds)),
                        ":now": encode_timestamp(now),
                        ":one": 1,
                    },
                    on_failed=_stale,
                    return_values="NONE",
                )
            except _Stale as stale:
                if not _owned_by(stale.old, owner):
                    self._forget(job_id, owner)
                    return JobStatus.RUNNING  # the lease was lost
            else:
                # Same message, delivered again when the retry is due: no new message.
                await self._visibility(job_id, self._forget(job_id, owner), retry_in_seconds)
                return JobStatus.QUEUED
        try:
            await self._table.update(
                keys.job(job_id),
                update=f"SET #s = :dead, last_error = :code, {_TERMINAL_SET} "
                f"REMOVE {_TERMINAL_REMOVE}",
                condition=owned,
                names=_S,
                values={**base, ":dead": JobStatus.DEAD.value, **self._terminal_values()},
                on_failed=_Stale(None),
                return_values="NONE",
            )
        except _Stale:
            self._forget(job_id, owner)
            return JobStatus.RUNNING
        await self._delete(job_id, self._forget(job_id, owner))
        return JobStatus.DEAD

    # Dispatcher: relay and sweep (J8) ----------------------------------------------------------

    async def dispatch_pending(self) -> int:
        """Send a message for every due outbox job (`GSI3PK = DISPATCH`); returns how many."""
        now = self._clock.now()
        rows = await self._table.query(
            key_condition="GSI3PK = :p AND GSI3SK <= :bound",
            values={":p": keys.GSI_DISPATCH, ":bound": encode_timestamp(now) + "#~"},
            index="GSI3",
            limit=RELAY_BATCH,
            page_size=RELAY_BATCH,
        )
        sent = 0
        for job_id, item in await self._reread(rows):  # the index is eventual
            if item is None or item.get("GSI3PK") != keys.GSI_DISPATCH:
                continue
            if await self._send(job_id):
                await self._mark_dispatched(job_id, keys.GSI_DISPATCH)
                sent += 1
        return sent

    async def sweep(self) -> int:
        """Re-send a message for every non-terminal job that is overdue by more than the grace
        period and was not sent within it (its message was lost, deleted or dead-lettered)."""
        now = self._clock.now()
        rows = await self._table.query(
            key_condition="GSI3PK = :p", values={":p": keys.GSI_SWEEP}, index="GSI3"
        )
        sent = 0
        for job_id, item in await self._reread(rows):
            if item is None or item.get("GSI3PK") != keys.GSI_SWEEP or not self._overdue(item, now):
                continue
            if await self._send(job_id):
                await self._mark_dispatched(job_id, keys.GSI_SWEEP)
                sent += 1
                _log.info("job_message_resent", job_id=str(job_id))
        return sent

    async def _reread(self, rows: list[Item]) -> list[tuple[uuid.UUID, Item | None]]:
        """The job items behind GSI3 hits, strongly consistent, in one `BatchGetItem` per 100
        jobs instead of one `GetItem` each (same read units, far fewer requests)."""
        job_ids = _ids(rows)
        found = await self._table.batch_get(keys.job(job_id) for job_id in job_ids)
        return [(job_id, found.get(keys.job(job_id))) for job_id in job_ids]

    def _overdue(self, item: Item, now: datetime) -> bool:
        status = decode_enum(JobStatus, item.get("status"), "status")
        if status is JobStatus.QUEUED:
            deadline = decode_timestamp(item.get("available_at"), "available_at")
        elif status is JobStatus.RUNNING:
            deadline = decode_timestamp(item.get("lease_expires_at"), "lease_expires_at")
        else:
            return False
        sent = item.get("dispatched_at")
        if sent is not None and decode_timestamp(sent, "dispatched_at") + self._grace >= now:
            return False
        return deadline + self._grace < now


def _owned_by(old: Item | None, owner: str) -> bool:
    return (
        old is not None
        and old.get("status") == JobStatus.RUNNING.value
        and old.get("lease_owner") == owner
    )


def _ids(rows: list[Item]) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    for row in rows:
        pk = row.get("PK")
        if isinstance(pk, str) and pk.startswith("JOB#"):
            try:
                found.append(uuid.UUID(pk.removeprefix("JOB#")))
            except ValueError:
                continue
    return found


def _code(exc: ClientError | BotoCoreError) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        return str(error.get("Code") or "ClientError")
    return type(exc).__name__


__all__ = ["EXHAUSTED", "DynamoSqsJobQueue"]
