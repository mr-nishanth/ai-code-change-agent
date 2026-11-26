"""Jira outcome reporting: one comment per attempt outcome, from an application service only.

This is the only Jira write in the platform. It is disabled unless JIRA_REPORTING_ENABLED=true;
it never changes status, fields or anything else. Whether the configured API token may add
comments is an external verification item (docs/SETUP-JIRA-WEBHOOK.md)."""

import re
from enum import StrEnum

import httpx

from ai_code_change_agent.integrations.jira.authentication import JiraAuthenticator

_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,31}-[0-9]{1,10}")
MAX_COMMENT = 8_000


class ReportOutcome(StrEnum):
    DISABLED = "DISABLED"
    POSTED = "POSTED"
    FAILED = "FAILED"


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

    def __repr__(self) -> str:
        return f"JiraOutcomeReporter(enabled={self._enabled})"

    async def report(self, issue_key: str, text: str) -> ReportOutcome:
        if not self._enabled:
            return ReportOutcome.DISABLED
        if not _KEY.fullmatch(issue_key):
            return ReportOutcome.FAILED
        body = {"body": _adf(text[:MAX_COMMENT])}
        try:
            async with httpx.AsyncClient(
                base_url=str(self._base),
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"/rest/api/3/issue/{issue_key}/comment",
                    json=body,
                    headers={
                        **(self._auth.headers() if self._auth else {}),
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError:
            return ReportOutcome.FAILED
        return ReportOutcome.POSTED if response.status_code == 201 else ReportOutcome.FAILED
