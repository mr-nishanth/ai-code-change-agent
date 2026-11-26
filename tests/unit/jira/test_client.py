from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ai_code_change_agent.application.jira.errors import (
    JiraAuthenticationError,
    JiraIntegrationError,
    JiraIssueNotFoundError,
    JiraPermissionError,
    JiraRateLimitError,
    JiraResponseError,
    JiraServiceUnavailableError,
    JiraTimeoutError,
)
from ai_code_change_agent.core.logging import configure_logging
from tests.support.jira_rest import BASE_URL, TOKEN, json_response, make_client


async def _get(client: Any) -> dict[str, Any]:
    result: dict[str, Any] = await client.get_json(
        "/rest/api/3/issue/TST-1", params={"fields": "summary"}, operation="get_issue"
    )
    return result


async def test_success_sends_auth_and_only_get() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response({"ok": True})

    client = make_client(handler)
    assert await _get(client) == {"ok": True}
    request = seen[0]
    assert request.method == "GET"
    assert request.url.host == "tenant.atlassian.net"
    assert request.url.scheme == "https"
    assert request.url.params["fields"] == "summary"
    assert request.headers["Authorization"].startswith("Basic ")
    assert request.headers["Accept"] == "application/json"
    assert "ai-code-change-agent" in request.headers["User-Agent"]
    await client.aclose()
    assert client.is_closed


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, JiraAuthenticationError),
        (403, JiraPermissionError),
        (404, JiraIssueNotFoundError),
        (400, JiraResponseError),
        (410, JiraResponseError),
        (501, JiraServiceUnavailableError),
    ],
)
async def test_non_retryable_statuses(status: int, error: type[JiraIntegrationError]) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="SECRET-BODY-FROM-JIRA")

    with pytest.raises(error) as exc:
        await _get(make_client(handler))
    assert calls == 1
    assert exc.value.status_code == status
    assert "SECRET-BODY" not in str(exc.value)
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_transient_status_retried_then_succeeds(status: int) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status) if calls <= 3 else json_response({"ok": 1})

    assert await _get(make_client(handler, sleeps=sleeps)) == {"ok": 1}
    assert calls == 4
    assert sleeps == [0.5, 1.0, 2.0]  # exponential, jitter stubbed to its cap


async def test_retry_exhaustion_is_bounded() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(JiraServiceUnavailableError) as exc:
        await _get(make_client(handler, max_retries=2))
    assert calls == 3
    assert exc.value.retryable is True


async def test_zero_retries_means_single_attempt() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    with pytest.raises(JiraServiceUnavailableError):
        await _get(make_client(handler, max_retries=0))
    assert calls == 1


async def test_backoff_is_capped() -> None:
    sleeps: list[float] = []
    with pytest.raises(JiraServiceUnavailableError):
        await _get(make_client(lambda _: httpx.Response(503), max_retries=8, sleeps=sleeps))
    assert max(sleeps) == 8.0
    assert len(sleeps) == 8


async def test_rate_limit_honours_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429, headers={"Retry-After": "7"})
            if calls == 1
            else json_response({"ok": 1})
        )

    assert await _get(make_client(handler, sleeps=sleeps)) == {"ok": 1}
    assert sleeps == [7.0]


async def test_rate_limit_with_huge_retry_after_fails_fast() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with pytest.raises(JiraRateLimitError) as exc:
        await _get(make_client(handler))
    assert calls == 1
    assert exc.value.retry_after_seconds == 3600


@pytest.mark.parametrize("value", ["abc", "-5", "", "1e3"])
async def test_rate_limit_garbage_retry_after_falls_back_to_backoff(value: str) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return (
            httpx.Response(429, headers={"Retry-After": value})
            if calls == 1
            else json_response({"ok": 1})
        )

    await _get(make_client(handler, sleeps=sleeps))
    assert sleeps == [0.5]


async def test_rate_limit_exhausted() -> None:
    with pytest.raises(JiraRateLimitError):
        await _get(make_client(lambda _: httpx.Response(429), max_retries=1))


async def test_connection_error_retried_then_unavailable() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("boom internal-host:5432", request=request)

    with pytest.raises(JiraServiceUnavailableError) as exc:
        await _get(make_client(handler, max_retries=2))
    assert calls == 3
    assert "internal-host" not in str(exc.value)


async def test_timeout_retried_then_timeout_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(JiraTimeoutError) as exc:
        await _get(make_client(handler, max_retries=1))
    assert calls == 2
    assert exc.value.retryable is True


async def test_transient_error_then_success() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("x", request=request)
        return json_response({"ok": 1})

    assert await _get(make_client(handler)) == {"ok": 1}


async def test_malformed_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"{not json", headers={"content-type": "application/json"}
        )

    with pytest.raises(JiraResponseError):
        await _get(make_client(handler))


@pytest.mark.parametrize("body", [b"[1,2]", b'"str"', b"null", b"5"])
async def test_non_object_json_rejected(body: bytes) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    with pytest.raises(JiraResponseError):
        await _get(make_client(handler))


async def test_non_json_content_type_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<html>login</html>", headers={"content-type": "text/html"}
        )

    with pytest.raises(JiraResponseError):
        await _get(make_client(handler))


async def test_oversized_by_content_length() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"{" + b" " * 5000 + b"}", headers={"content-type": "application/json"}
        )

    with pytest.raises(JiraResponseError) as exc:
        await _get(make_client(handler, max_bytes=1024))
    assert "response_too_large" in str(exc.value)


async def test_oversized_when_streamed_without_length() -> None:
    async def body() -> AsyncIterator[bytes]:
        for _ in range(100):
            yield b" " * 100

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body(), headers={"content-type": "application/json"})

    with pytest.raises(JiraResponseError) as exc:
        await _get(make_client(handler, max_bytes=1024))
    assert "response_too_large" in str(exc.value)


async def test_redirects_are_not_followed_and_not_leaked() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://evil.invalid/steal"})

    with pytest.raises(JiraResponseError) as exc:
        await _get(make_client(handler))
    assert hosts == ["tenant.atlassian.net"]
    assert "unexpected_redirect" in str(exc.value)


async def test_logs_never_contain_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("DEBUG", json_output=True)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="SECRET-BODY")

    with pytest.raises(JiraServiceUnavailableError):
        await _get(make_client(handler, max_retries=1))
    out = capsys.readouterr().out
    assert "jira_request" in out
    for secret in (TOKEN, "Basic ", "SECRET-BODY", "svc@corp.invalid"):
        assert secret not in out


def test_base_url_constant_is_https() -> None:
    assert BASE_URL.startswith("https://")
