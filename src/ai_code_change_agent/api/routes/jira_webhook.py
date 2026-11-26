"""POST /webhooks/jira. Thin: size cap -> use case -> HTTP mapping. Acknowledges quickly: the
accepted Story becomes a durable job in the same transaction; no agent work happens here."""

import re
from typing import Any

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ai_code_change_agent.application.webhooks.receive import DELIVERY_ID_HEADER, ReceiveJiraWebhook
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.webhooks.models import (
    PersistenceError,
    RejectionCode,
    WebhookRejected,
)

router = APIRouter(prefix="/webhooks", tags=["jira-webhook"])
_log = get_logger("webhook")

_REJECTIONS: dict[RejectionCode, tuple[int, str]] = {
    RejectionCode.CONTENT_TYPE_UNSUPPORTED: (415, "unsupported_media_type"),
    RejectionCode.BODY_MISSING: (400, "body_missing"),
    RejectionCode.SIGNATURE_MISSING: (401, "unauthorized"),
    RejectionCode.SIGNATURE_MALFORMED: (401, "unauthorized"),
    RejectionCode.SIGNATURE_UNSUPPORTED_METHOD: (401, "unauthorized"),
    RejectionCode.SIGNATURE_MISMATCH: (401, "unauthorized"),
    RejectionCode.DELIVERY_ID_INVALID: (400, "delivery_id_invalid"),
    RejectionCode.PAYLOAD_INVALID: (400, "payload_invalid"),
}


class _TooLarge(Exception):  # noqa: N818
    pass


def _error(status: int, code: str) -> JSONResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    return JSONResponse({"error": {"code": code, "request_id": request_id}}, status_code=status)


def _printable(value: str | None) -> str | None:
    return None if value is None else re.sub(r"[^ -~]", "?", value)[:64]


async def _read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > limit:
        raise _TooLarge
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _TooLarge
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/jira")
async def receive_jira_webhook(request: Request) -> JSONResponse:
    service: ReceiveJiraWebhook | None = request.app.state.webhook_service
    if service is None:
        _log.error("webhook_unavailable", reason="not_configured")
        return _error(503, "webhook_not_configured")

    delivery = _printable(request.headers.get(DELIVERY_ID_HEADER))
    try:
        body = await _read_capped(request, request.app.state.settings.jira_webhook_max_body_bytes)
    except _TooLarge:
        _log.warning("webhook_rejected", reason="payload_too_large", delivery_id=delivery)
        return _error(413, "payload_too_large")

    try:
        result = await service.handle(body, request.headers)
    except WebhookRejected as rejected:
        status, code = _REJECTIONS[rejected.code]
        _log.warning(
            "webhook_rejected",
            reason=rejected.code.value,
            delivery_id=delivery,
            body_bytes=len(body),
        )
        return _error(status, code)
    except PersistenceError as exc:
        _log.error("webhook_failed", reason="database_error", delivery_id=delivery, error=str(exc))
        return _error(500, "internal_error")

    content: dict[str, Any] = {"status": result.outcome.value, "deliveryId": result.delivery_id}
    if result.reason:
        content["reason"] = result.reason
    return JSONResponse(content, status_code=200)
