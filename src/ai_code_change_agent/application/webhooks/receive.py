"""ReceiveJiraWebhook: verify -> validate -> scope -> record. Never starts agents."""

import hashlib
import re
from collections.abc import Mapping

from ai_code_change_agent.application.webhooks.payload import envelope_event, issue_ref, load_json
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.webhooks.models import (
    IssueRef,
    Outcome,
    ProcessingStatus,
    ReceivedDelivery,
    RejectionCode,
    WebhookRejected,
    WebhookResult,
)
from ai_code_change_agent.domain.webhooks.policy import ScopePolicy
from ai_code_change_agent.domain.webhooks.ports import WebhookRepository, WebhookVerifier

DELIVERY_ID_HEADER = "x-atlassian-webhook-identifier"
RETRY_HEADER = "x-atlassian-webhook-retry"
FLOW_HEADER = "x-atlassian-webhook-flow"
_DELIVERY_ID = re.compile(r"[!-~]{1,255}")
_FLOWS = frozenset({"Primary", "Secondary"})
_log = get_logger("webhook")


def _printable(value: str, limit: int = 100) -> str:
    return re.sub(r"[^ -~]", "?", value)[:limit]


def _retry_count(value: str | None) -> int:
    if value is not None and value.isascii() and value.isdigit() and int(value) < 1000:
        return int(value)
    return 0


class ReceiveJiraWebhook:
    def __init__(
        self, verifier: WebhookVerifier, repository: WebhookRepository, policy: ScopePolicy
    ) -> None:
        self._verifier = verifier
        self._repository = repository
        self._policy = policy

    async def handle(self, raw_body: bytes, headers: Mapping[str, str]) -> WebhookResult:
        h = {k.lower(): v for k, v in headers.items()}
        if h.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise WebhookRejected(RejectionCode.CONTENT_TYPE_UNSUPPORTED)

        verification = self._verifier.verify(raw_body, h)  # over the exact raw bytes
        if not verification.ok:
            raise WebhookRejected(verification.failure or RejectionCode.SIGNATURE_MISMATCH)
        if not raw_body:
            raise WebhookRejected(RejectionCode.BODY_MISSING)

        delivery_id = h.get(DELIVERY_ID_HEADER)
        if delivery_id is None or not _DELIVERY_ID.fullmatch(delivery_id):
            raise WebhookRejected(RejectionCode.DELIVERY_ID_INVALID)

        data = load_json(raw_body)
        event = envelope_event(data)
        flow = h.get(FLOW_HEADER)
        common = {
            "delivery_id": delivery_id,
            "payload_sha256": hashlib.sha256(raw_body).hexdigest(),
            "event_type": event,
            "retry_count": _retry_count(h.get(RETRY_HEADER)),
            "flow": flow if flow in _FLOWS else None,
        }

        issue: IssueRef | None = None
        ignore = self._policy.check_event(event)
        if ignore is None:
            issue = issue_ref(data)
            ignore = self._policy.check_issue(issue)

        delivery = ReceivedDelivery(
            **common,  # type: ignore[arg-type]
            status=ProcessingStatus.IGNORED if ignore else ProcessingStatus.ACCEPTED,
            ignore_reason=ignore,
            issue=issue,
        )
        outcome = await self._repository.record(delivery)
        reason = ignore.value if (outcome is Outcome.IGNORED and ignore) else None
        _log.info(
            "webhook_processed",
            outcome=outcome.value,
            reason=reason,
            delivery_id=delivery_id,
            event_type=_printable(event),
            issue_key=issue.issue_key if issue else None,
            project_key=_printable(issue.project_key, 64) if issue else None,
        )
        return WebhookResult(outcome=outcome, delivery_id=delivery_id, reason=reason)
