"""DynamoDB adapter for webhook intake (DYNAMODB-ACCESS-PATTERNS.md 1, W1-W7).

An accepted delivery is ONE `TransactWriteItems` (W1), every item created with
`attribute_not_exists(PK)`, in this order:

0. `DELIVERY#<delivery_id>/DELIVERY` (W2): written once, with its final `processing_result` and
   `request_id`; it already exists -> `Outcome.DUPLICATE` (a redelivery).
1. `JIRAISSUE#<jira_issue_id>/GUARD` (W3) -> `request_id`, one request per Story; it already exists
   -> the transaction is cancelled and the guard's `ALL_OLD` image names the existing request (W4,
   no extra read; a consistent `GetItem` only if no image came back). The delivery is then written
   alone, conditionally, as `DUPLICATE_ISSUE` linked to that request (W5) -> `Outcome.DUPLICATE`.
2. `REQ#<rid>/REQUEST` (`change_store.request_item`).
3. `JOBKEY#code_change.intake:<rid>/GUARD` and 4. `JOB#<jid>/JOB` (W6, `job_items.new_job`): the
   intake job, QUEUED, with its GSI3 outbox keys. The API never calls SQS; the dispatcher in the
   worker relays the job.

An ignored delivery is one conditional `PutItem` of the delivery item. Deliveries are never read
back (W7), so `request_id` stays a plain attribute. A delivery item only has to outlive Jira's
redelivery window (hours): it carries `expires_at` (TTL, `ACCA_JIRA_DELIVERY_RETENTION_DAYS`,
default 30 days). The Story guard and the job dedupe guard stay permanent, so a redelivery after
that still creates nothing. Every failure that is not one of these
outcomes is `PersistenceError` whose message is a code or class name, never driver text."""

import uuid
from datetime import timedelta
from typing import Any

from ai_code_change_agent.domain.changes.models import CodeChangeRequest
from ai_code_change_agent.domain.webhooks.models import (
    Outcome,
    PersistenceError,
    ProcessingStatus,
    ReceivedDelivery,
)
from ai_code_change_agent.persistence.dynamodb import keys
from ai_code_change_agent.persistence.dynamodb.change_store import request_item
from ai_code_change_agent.persistence.dynamodb.clock import Clock, SystemClock
from ai_code_change_agent.persistence.dynamodb.codec import encode_epoch_seconds, encode_timestamp
from ai_code_change_agent.persistence.dynamodb.errors import Item
from ai_code_change_agent.persistence.dynamodb.job_items import new_job
from ai_code_change_agent.persistence.dynamodb.table import Table

INTAKE_JOB_KIND = "code_change.intake"
DELIVERY_RETENTION_DAYS = 30  # Settings.jira_delivery_retention_days
_NEW = "attribute_not_exists(PK)"


class _DeliveryExists(Exception):  # noqa: N818 - internal signal, not an error
    """The delivery id is already recorded."""


class _IssueTaken(Exception):  # noqa: N818 - internal signal, not an error
    """Another delivery already created this Story's request (from the guard's old image)."""

    def __init__(self, request_id: str | None) -> None:
        super().__init__("issue_taken")
        self.request_id = request_id


def _issue_taken(old: Item | None) -> BaseException:
    request_id = old.get("request_id") if old is not None else None
    return _IssueTaken(request_id if isinstance(request_id, str) else None)


class DynamoWebhookRepository:
    def __init__(
        self,
        table: Table,
        *,
        job_max_attempts: int = 3,
        clock: Clock | None = None,
        delivery_retention_days: int = DELIVERY_RETENTION_DAYS,
    ) -> None:
        if isinstance(delivery_retention_days, bool) or not 1 <= delivery_retention_days <= 365:
            raise ValueError("delivery_retention_days must be 1..365")
        self._table = table
        self._max_attempts = job_max_attempts
        self._clock: Clock = clock or SystemClock()
        self._retention = timedelta(days=delivery_retention_days)

    async def record(self, delivery: ReceivedDelivery) -> Outcome:
        try:
            return await self._record(delivery)
        except PersistenceError:
            raise  # already a code without driver text
        except Exception as exc:
            # Expose the class name only; nothing from the driver is chained.
            raise PersistenceError(type(exc).__name__) from None

    async def _record(self, delivery: ReceivedDelivery) -> Outcome:
        issue = delivery.issue
        if delivery.status is ProcessingStatus.IGNORED or issue is None:
            item = self._delivery_item(delivery, "IGNORED", None)
            return await self._put_delivery(item, Outcome.IGNORED)

        now = self._clock.now()
        request = CodeChangeRequest(
            uuid.uuid4(), issue.issue_id, issue.issue_key, issue.project_key
        )
        job = new_job(
            INTAKE_JOB_KIND,
            {
                "code_change_request_id": str(request.id),
                "jira_issue_id": issue.issue_id,
                "jira_issue_key": issue.issue_key,
            },
            dedupe_key=f"{INTAKE_JOB_KIND}:{request.id}",
            max_attempts=self._max_attempts,
            now=now,
        )
        tx = self._table.transaction()
        tx.put(
            self._delivery_item(delivery, "ACCEPTED", str(request.id)),
            condition=_NEW,
            on_failed=_DeliveryExists(),
        )
        tx.put(
            {
                **keys.issue_guard(issue.issue_id).as_dict(),
                "entity": "jira_issue_guard",
                "request_id": str(request.id),
                "jira_issue_key": issue.issue_key,
                "created_at": encode_timestamp(now),
            },
            condition=_NEW,
            on_failed=_issue_taken,
        )
        tx.put(request_item(request, created_at=now), condition=_NEW)
        tx.put(job.guard, condition=_NEW)
        tx.put(job.job, condition=_NEW)
        try:
            await tx.commit()
        except _DeliveryExists:
            return Outcome.DUPLICATE
        except _IssueTaken as taken:
            existing = taken.request_id or await self._existing_request(issue.issue_id)
            item = self._delivery_item(delivery, "DUPLICATE_ISSUE", existing)
            return await self._put_delivery(item, Outcome.DUPLICATE)
        return Outcome.ACCEPTED

    async def _existing_request(self, jira_issue_id: str) -> str:
        guard = await self._table.get(keys.issue_guard(jira_issue_id), consistent=True)
        request_id = guard.get("request_id") if guard is not None else None
        if not isinstance(request_id, str):
            raise PersistenceError("issue_guard_missing")
        return request_id

    async def _put_delivery(self, item: Item, outcome: Outcome) -> Outcome:
        try:
            await self._table.put(item, condition=_NEW, on_failed=_DeliveryExists())
        except _DeliveryExists:
            return Outcome.DUPLICATE
        return outcome

    def _delivery_item(
        self, delivery: ReceivedDelivery, result: str, request_id: str | None
    ) -> Item:
        issue = delivery.issue
        now = self._clock.now()
        item: dict[str, Any] = {
            **keys.delivery(delivery.delivery_id).as_dict(),
            "entity": "jira_delivery",
            "delivery_id": delivery.delivery_id,
            "payload_sha256": delivery.payload_sha256,
            "event_type": delivery.event_type,
            "retry_count": delivery.retry_count,
            "verification_result": "VERIFIED",
            "processing_result": result,
            "received_at": encode_timestamp(now),
            "expires_at": encode_epoch_seconds(now + self._retention),
        }
        optional = {
            "flow": delivery.flow,
            "ignore_reason": delivery.ignore_reason.value if delivery.ignore_reason else None,
            "jira_issue_id": issue.issue_id if issue else None,
            "jira_issue_key": issue.issue_key if issue else None,
            "jira_project_key": issue.project_key if issue else None,
            "jira_issue_type": issue.issue_type if issue else None,
            "request_id": request_id,
        }
        item.update({name: value for name, value in optional.items() if value is not None})
        return item


__all__ = ["INTAKE_JOB_KIND", "DynamoWebhookRepository"]
