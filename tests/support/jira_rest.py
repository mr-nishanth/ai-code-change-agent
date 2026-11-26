"""Synthetic Jira Cloud REST payloads and an HTTP client factory for tests (never real data)."""

from collections.abc import Callable
from typing import Any

import httpx
from pydantic import SecretStr

from ai_code_change_agent.integrations.jira.authentication import BasicAuthenticator
from ai_code_change_agent.integrations.jira.client import JiraClientConfig, JiraHttpClient

BASE_URL = "https://tenant.atlassian.net"
TOKEN = "tok-0123456789abcdef-synthetic"
EMAIL = "svc@corp.invalid"


def adf(*paragraphs: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs
        ],
    }


def user(
    account_id: str = "acc-1", name: str = "Ada Example", *, active: bool = True
) -> dict[str, Any]:
    return {
        "accountId": account_id,
        "displayName": name,
        "active": active,
        "emailAddress": "ada@corp.invalid",
    }


def issue_payload(key: str = "TST-1", issue_id: str = "10001", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": "Synthetic story",
        "description": adf("Do the thing"),
        "issuetype": {"id": "10", "name": "Story"},
        "project": {"id": "100", "key": "TST", "name": "Test Project"},
        "status": {"name": "To Do"},
        "priority": {"name": "Medium"},
        "labels": ["alpha", "beta"],
        "components": [{"name": "core"}, {"name": "api"}],
        "assignee": user(),
        "reporter": user("acc-2", "Bob Example"),
        "created": "2026-01-02T03:04:05.000+0000",
        "updated": "2026-01-03T04:05:06.123+0000",
    }
    base.update(fields)
    return {"id": issue_id, "key": key, "fields": base}


def comment_payload(cid: str = "1", text: str = "hello", **over: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": cid,
        "author": user(),
        "body": adf(text),
        "created": "2026-01-02T03:04:05.000+0000",
        "updated": "2026-01-02T03:04:06.000+0000",
    }
    out.update(over)
    return out


def comments_page(
    items: list[dict[str, Any]], *, start_at: int = 0, total: int | None = None
) -> dict[str, Any]:
    return {
        "startAt": start_at,
        "maxResults": len(items),
        "total": len(items) if total is None else total,
        "comments": items,
    }


def json_response(
    body: Any, status: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    max_retries: int = 3,
    max_bytes: int = 5_242_880,
    sleeps: list[float] | None = None,
) -> JiraHttpClient:
    recorded = sleeps if sleeps is not None else []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    config = JiraClientConfig(
        base_url=BASE_URL,
        connect_timeout_seconds=5,
        read_timeout_seconds=20,
        max_retries=max_retries,
        max_response_bytes=max_bytes,
    )
    return JiraHttpClient(
        config,
        BasicAuthenticator(EMAIL, SecretStr(TOKEN)),
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
        jitter=lambda cap: cap,
    )
