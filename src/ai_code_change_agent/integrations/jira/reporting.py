"""Jira outcome reporting: one comment per attempt outcome, from an application service only.

This is the only Jira write in the platform. It is disabled unless JIRA_REPORTING_ENABLED=true;
it never changes status, fields or anything else. Whether the configured API token may add
comments is an external verification item (docs/SETUP-JIRA-WEBHOOK.md)."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from ai_code_change_agent.integrations.jira.authentication import JiraAuthenticator

_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,31}-[0-9]{1,10}")
MAX_COMMENT = 8_000


class ReportOutcome(StrEnum):
    DISABLED = "DISABLED"
    POSTED = "POSTED"
    FAILED = "FAILED"  # retryable only: 429, 5xx, timeout, transport error
    PERMANENT_FAILURE = "PERMANENT_FAILURE"  # 400/401/403/404/..., invalid key: never repost


def _adf(text: str) -> dict[str, object]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in text.splitlines()
            if line.strip()
        ],
    }


class CommentStatus(StrEnum):
    POSTED = "POSTED"  # Jira answered 201 (or the comment was found on the issue)
    RETRYABLE = "RETRYABLE"  # 429, 5xx, timeout, network: the outcome may be unknown
    PERMANENT = "PERMANENT"  # 400/401/403/404, invalid key: retrying cannot help
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class CommentResult:
    status: CommentStatus
    comment_id: str | None = None
    http_status: int | None = None
    error: str | None = None
    retry_after: float | None = None  # seconds, from a 429/503 Retry-After header


_PERMANENT_HTTP = frozenset({400, 401, 403, 404, 405, 410, 413, 422})
_COMMENT_ID = re.compile(r"[0-9]{1,20}")
_RETRY_AFTER_MAX = 3600.0


def _retry_after(response: httpx.Response | None) -> float | None:
    """Retry-After in delta-seconds (the form Jira sends); anything else is ignored."""
    raw = response.headers.get("Retry-After") if response is not None else None
    try:
        seconds = float(raw) if raw is not None else None
    except ValueError:
        return None
    if seconds is None or not 0 <= seconds <= _RETRY_AFTER_MAX:
        return None
    return seconds


def _classify(code: int, response: httpx.Response | None = None) -> CommentResult:
    if code in _PERMANENT_HTTP:
        return CommentResult(CommentStatus.PERMANENT, http_status=code, error=f"http_{code}")
    return CommentResult(
        CommentStatus.RETRYABLE,
        http_status=code,
        error=f"http_{code}",
        retry_after=_retry_after(response),
    )


def _adf_text(node: Any) -> str:
    if isinstance(node, dict):
        text = node.get("text")
        own = text if isinstance(text, str) else ""
        return own + " " + " ".join(_adf_text(c) for c in node.get("content") or [])
    if isinstance(node, list):
        return " ".join(_adf_text(c) for c in node)
    return ""


class JiraOutcomeReporter:
    def __init__(
        self,
        *,
        enabled: bool,
        base_url: str | None,
        authenticator: JiraAuthenticator | None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._enabled = enabled and base_url is not None and authenticator is not None
        self._base = base_url
        self._auth = authenticator
        self._transport = transport
        self._timeout = timeout_seconds
        self._account: str | None = None

    def __repr__(self) -> str:
        return f"JiraOutcomeReporter(enabled={self._enabled})"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def timeout_seconds(self) -> float:
        return self._timeout

    async def _account_id(self, client: httpx.AsyncClient) -> str | None:
        """The reporting account's id (`GET /rest/api/3/myself`, cached): only its comments can
        carry a genuine report marker. None when Jira did not say (the lookup then fails)."""
        if self._account is None:
            try:
                response = await client.get("/rest/api/3/myself", headers=self._headers())
                account = response.json().get("accountId") if response.status_code == 200 else None
            except (httpx.HTTPError, ValueError, AttributeError):
                account = None
            if isinstance(account, str) and account:
                self._account = account
        return self._account

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=str(self._base),
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
        )

    def _headers(self) -> dict[str, str]:
        return {**(self._auth.headers() if self._auth else {}), "Accept": "application/json"}

    async def report(self, issue_key: str, text: str) -> ReportOutcome:
        result = await self.post_comment(issue_key, text)
        return {
            CommentStatus.DISABLED: ReportOutcome.DISABLED,
            CommentStatus.POSTED: ReportOutcome.POSTED,
            CommentStatus.PERMANENT: ReportOutcome.PERMANENT_FAILURE,
        }.get(result.status, ReportOutcome.FAILED)

    async def post_comment(self, issue_key: str, text: str) -> CommentResult:
        """One comment POST, never retried here: a timeout may hide a comment Jira created, so
        the caller reconciles with find_comment() before posting again."""
        if not self._enabled:
            return CommentResult(CommentStatus.DISABLED)
        if not _KEY.fullmatch(issue_key):
            return CommentResult(CommentStatus.PERMANENT, error="issue_key_invalid")
        try:
            async with self._client() as client:
                response = await client.post(
                    f"/rest/api/3/issue/{issue_key}/comment",
                    json={"body": _adf(text[:MAX_COMMENT])},
                    headers=self._headers(),
                )
        except httpx.TimeoutException:
            return CommentResult(CommentStatus.RETRYABLE, error="timeout")
        except httpx.HTTPError:
            return CommentResult(CommentStatus.RETRYABLE, error="transport_error")
        if response.status_code != 201:
            return _classify(response.status_code, response)
        try:
            raw = response.json().get("id")
        except ValueError:
            raw = None
        cid = str(raw) if raw is not None and _COMMENT_ID.fullmatch(str(raw)) else None
        return CommentResult(CommentStatus.POSTED, comment_id=cid, http_status=201)

    async def find_comment(self, issue_key: str, marker: str) -> CommentResult | None:
        """The newest comment by the reporting account (`/myself`) whose text contains marker (a
        report id), or None when Jira confirms there is none; a user who pastes the marker into a
        comment of their own cannot suppress the report. A failed lookup is returned as
        RETRYABLE/PERMANENT: the caller must not post while the outcome is unknown."""
        if not self._enabled:
            return CommentResult(CommentStatus.DISABLED)
        if not _KEY.fullmatch(issue_key):
            return CommentResult(CommentStatus.PERMANENT, error="issue_key_invalid")
        try:
            async with self._client() as client:
                account = await self._account_id(client)
                if account is None:
                    return CommentResult(CommentStatus.RETRYABLE, error="lookup_identity_unknown")
                response = await client.get(
                    f"/rest/api/3/issue/{issue_key}/comment",
                    params={"orderBy": "-created", "maxResults": 100},
                    headers=self._headers(),
                )
        except httpx.TimeoutException:
            return CommentResult(CommentStatus.RETRYABLE, error="lookup_timeout")
        except httpx.HTTPError:
            return CommentResult(CommentStatus.RETRYABLE, error="lookup_transport_error")
        if response.status_code != 200:
            found = _classify(response.status_code, response)
            return CommentResult(
                found.status,
                http_status=found.http_status,
                error="lookup_" + str(found.error),
                retry_after=found.retry_after,
            )
        try:
            comments = response.json().get("comments") or []
        except ValueError:
            return CommentResult(CommentStatus.RETRYABLE, error="lookup_invalid_json")
        for comment in comments:  # only the reporting account's own comments can be the report
            if not isinstance(comment, dict):
                continue
            author = comment.get("author")
            if not isinstance(author, dict) or author.get("accountId") != account:
                continue
            if marker in _adf_text(comment.get("body")):
                cid = str(comment.get("id", ""))
                return CommentResult(
                    CommentStatus.POSTED,
                    comment_id=cid if _COMMENT_ID.fullmatch(cid) else None,
                    http_status=200,
                )
        return None
