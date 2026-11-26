"""Async GET-only HTTP client for Jira Cloud REST.

One reusable `httpx.AsyncClient` (connection pool) bound to the single configured origin.
Redirects are never followed; error bodies are never read; response size is capped while
streaming; retries are bounded and only for transient failures.
"""

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ai_code_change_agent import __version__
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
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.integrations.jira.authentication import JiraAuthenticator

API_PREFIX = "/rest/api/3/"
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
_DIGITS = re.compile(r"[0-9]{1,7}")

log = get_logger("jira.client")


@dataclass(frozen=True)
class JiraClientConfig:
    base_url: str
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_retries: int
    max_response_bytes: int
    backoff_base_seconds: float = 0.5
    backoff_max_seconds: float = 8.0
    retry_after_max_seconds: float = 30.0


def _full_jitter(cap: float) -> float:
    return random.uniform(0.0, cap)  # noqa: S311 - backoff jitter, not security sensitive


class JiraHttpClient:
    def __init__(
        self,
        config: JiraClientConfig,
        authenticator: JiraAuthenticator,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] = _full_jitter,
    ) -> None:
        self._config = config
        self._auth = authenticator
        self._sleep = sleep
        self._jitter = jitter
        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "User-Agent": f"ai-code-change-agent/{__version__}",
            },
            transport=transport,
        )

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_json(
        self, path: str, *, params: dict[str, str | int] | None = None, operation: str
    ) -> dict[str, Any]:
        if not path.startswith(API_PREFIX) or ".." in path or "//" in path:
            msg = "path must be a plain Jira REST API path"
            raise ValueError(msg)
        attempt = 0
        while True:
            try:
                return await self._once(path, params, operation, attempt)
            except JiraIntegrationError as err:
                if not err.retryable or attempt >= self._config.max_retries:
                    raise
                delay = self._delay(attempt, err)
                if delay is None:
                    raise
                log.warning(
                    "jira_request_retry",
                    operation=operation,
                    attempt=attempt + 1,
                    reason=err.code,
                    delay_seconds=delay,
                )
                await self._sleep(delay)
                attempt += 1

    def _delay(self, attempt: int, err: JiraIntegrationError) -> float | None:
        cfg = self._config
        backoff = min(cfg.backoff_max_seconds, cfg.backoff_base_seconds * (2**attempt))
        delay = self._jitter(backoff)
        if isinstance(err, JiraRateLimitError) and err.retry_after_seconds is not None:
            if err.retry_after_seconds > cfg.retry_after_max_seconds:
                return None
            delay = max(delay, err.retry_after_seconds)
        return delay

    async def _once(
        self, path: str, params: dict[str, str | int] | None, operation: str, attempt: int
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            async with self._client.stream(
                "GET", path, params=params, headers=self._auth.headers()
            ) as response:
                log.info(
                    "jira_request",
                    operation=operation,
                    status=response.status_code,
                    attempt=attempt + 1,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
                return await self._handle(response, operation)
        except httpx.TimeoutException:
            raise JiraTimeoutError("request_timeout", operation=operation) from None
        except httpx.TransportError:
            raise JiraServiceUnavailableError("connection_error", operation=operation) from None

    async def _handle(self, response: httpx.Response, operation: str) -> dict[str, Any]:
        status = response.status_code
        if 200 <= status < 300:
            return await self._read_json(response, operation)
        if 300 <= status < 400:
            raise JiraResponseError("unexpected_redirect", operation=operation, status_code=status)
        if status == 401:
            raise JiraAuthenticationError(operation=operation, status_code=status)
        if status == 403:
            raise JiraPermissionError(operation=operation, status_code=status)
        if status == 404:
            raise JiraIssueNotFoundError(operation=operation, status_code=status)
        if status == 429:
            raise JiraRateLimitError(
                operation=operation,
                status_code=status,
                retry_after_seconds=self._retry_after(response),
            )
        if status >= 500:
            raise JiraServiceUnavailableError(
                "server_error",
                operation=operation,
                status_code=status,
                retryable=status in _RETRYABLE_STATUSES,
            )
        raise JiraResponseError("unexpected_status", operation=operation, status_code=status)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after", "").strip()
        return float(raw) if _DIGITS.fullmatch(raw) else None

    async def _read_json(self, response: httpx.Response, operation: str) -> dict[str, Any]:
        limit = self._config.max_response_bytes
        if "json" not in response.headers.get("content-type", "").lower():
            raise JiraResponseError("unexpected_content_type", operation=operation)
        declared = response.headers.get("content-length", "")
        if declared.isascii() and declared.isdigit() and int(declared) > limit:
            raise JiraResponseError("response_too_large", operation=operation)
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            buffer.extend(chunk)
            if len(buffer) > limit:
                raise JiraResponseError("response_too_large", operation=operation)
        try:
            data = json.loads(bytes(buffer))
        except (ValueError, RecursionError):
            raise JiraResponseError("malformed_json", operation=operation) from None
        if not isinstance(data, dict):
            raise JiraResponseError("unexpected_json_shape", operation=operation)
        return data
