"""Synthetic Jira Cloud webhook helpers. No real secrets or payloads."""

import hashlib
import hmac
import json
from typing import Any

SECRET = "test-secret-0123456789abcdef"
PROJECT = "TST"
ISSUE_TYPE = "Story"
EVENT = "jira:issue_created"


def sign(body: bytes, secret: str = SECRET, method: str = "sha256") -> str:
    return f"{method}={hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}"


def payload(
    *,
    issue_id: str = "10001",
    key: str = "TST-1",
    project: str = PROJECT,
    issue_type: str = ISSUE_TYPE,
    event: str = EVENT,
    summary: str = "Synthetic story",
) -> dict[str, Any]:
    return {
        "timestamp": 1700000000000,
        "webhookEvent": event,
        "issue": {
            "id": issue_id,
            "key": key,
            "fields": {
                "summary": summary,
                "project": {"key": project},
                "issuetype": {"name": issue_type},
            },
        },
    }


def body_bytes(data: dict[str, Any] | None = None, **kw: Any) -> bytes:
    return json.dumps(data if data is not None else payload(**kw), indent=2).encode()


def headers(
    body: bytes,
    *,
    delivery_id: str | None = "delivery-0001",
    signature: str | None = "auto",
    content_type: str | None = "application/json",
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    h: dict[str, str] = {}
    if content_type is not None:
        h["content-type"] = content_type
    if signature == "auto":
        h["x-hub-signature"] = sign(body)
    elif signature is not None:
        h["x-hub-signature"] = signature
    if delivery_id is not None:
        h["x-atlassian-webhook-identifier"] = delivery_id
    h.update(extra or {})
    return h
