"""Test doubles for durable Jira reporting: a scripted Jira comment endpoint (mock transport, never
the network) and an in-memory `JiraReportStore` with the DynamoDB store's conditions."""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from ai_code_change_agent.application.jira.reports import ReportClaim, ReportRecord, ReportState
from ai_code_change_agent.integrations.jira.authentication import BasicAuthenticator
from ai_code_change_agent.integrations.jira.reporting import JiraOutcomeReporter

BASE = "https://tenant.atlassian.net"
BOT = "acc-reporting-bot"  # the reporting account (/myself); comments carry it as author
# A scripted POST outcome: an HTTP status, (status, headers), "timeout" (nothing created),
# or "accepted_timeout" (Jira created the comment, the reply was lost).
Step = int | tuple[int, dict[str, str]] | str


def adf_text(body: dict[str, Any]) -> str:
    return "\n".join(
        node["text"]
        for para in body.get("content", [])
        for node in para.get("content", [])
        if isinstance(node, dict) and "text" in node
    )


@dataclass
class CommentJira:
    """Comments of any issue: POST answers from `script` (then 201), GET lists what was created
    (newest first); `lookups` scripts GET failures the same way."""

    script: list[Step] = field(default_factory=list)
    lookups: list[Step] = field(default_factory=list)
    # (issue path, text[, author account id]): authored by the reporting account unless given
    comments: list[tuple[str, ...]] = field(default_factory=list)
    posts: int = 0
    gets: int = 0

    def texts(self) -> list[str]:
        return [c[1] for c in self.comments]

    def _step(self, steps: list[Step]) -> Step | None:
        return steps.pop(0) if steps else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/comment"):
            self.posts += 1
            step = self._step(self.script)
            if step == "timeout":
                raise httpx.ReadTimeout("synthetic", request=request)
            if step is not None and step != "accepted_timeout":
                status, headers = step if isinstance(step, tuple) else (step, {})
                return httpx.Response(int(status), headers=headers, json={"errorMessages": ["x"]})
            text = adf_text(json.loads(request.read())["body"])
            self.comments.append((path, text))
            if step == "accepted_timeout":
                raise httpx.ReadTimeout("synthetic", request=request)
            return httpx.Response(201, json={"id": str(10000 + len(self.comments))})
        if request.method == "GET" and path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": BOT})
        if request.method == "GET" and path.endswith("/comment"):
            self.gets += 1
            step = self._step(self.lookups)
            if step == "timeout":
                raise httpx.ReadTimeout("synthetic", request=request)
            if step is not None:
                status, headers = step if isinstance(step, tuple) else (step, {})
                return httpx.Response(int(status), headers=headers, json={})
            found = [
                {"id": str(10001 + i), "author": {"accountId": author}, "body": {"type": "doc", "version": 1, "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": line}]}
                    for line in text.splitlines()
                ]}}
                for i, (p, text, author) in enumerate(
                    (c[0], c[1], c[2] if len(c) > 2 else BOT) for c in self.comments
                )
                if p == path
            ]  # fmt: skip
            return httpx.Response(200, json={"comments": list(reversed(found))})
        return httpx.Response(404, json={})

    def reporter(self, *, enabled: bool = True) -> JiraOutcomeReporter:
        return JiraOutcomeReporter(
            enabled=enabled,
            base_url=BASE,
            authenticator=BasicAuthenticator("svc@corp.invalid", SecretStr("tok-synthetic")),
            transport=httpx.MockTransport(self.handle),
        )


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def now(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class MemoryReportStore:
    """The `DynamoJiraReportStore` contract in memory (same conditions, same due rule)."""

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self.now = now or (lambda: datetime.now(UTC))
        self.items: dict[str, dict[str, Any]] = {}
        self.texts: dict[str, str] = {}

    async def create(self, report_id: str, issue_key: str, text: str, sha256: str) -> bool:
        if report_id in self.items:
            return False
        now = self.now()
        record = ReportRecord(report_id, issue_key, "", sha256, ReportState.PENDING, 0,
                              next_retry_at=now, created_at=now, updated_at=now)  # fmt: skip
        self.items[report_id] = {"record": record, "token": None, "claimed_at": None, "due": now}
        self.texts[report_id] = text
        return True

    async def text(self, report_id: str) -> str | None:
        return self.texts.get(report_id)

    async def claim(self, report_id: str, stale_seconds: int) -> ReportClaim | None:
        item = self.items.get(report_id)
        if item is None:
            return None
        rec: ReportRecord = item["record"]
        now = self.now()
        pending = rec.state is ReportState.PENDING and rec.next_retry_at is not None
        due = pending and rec.next_retry_at <= now  # type: ignore[operator]
        stale = rec.state is ReportState.IN_PROGRESS and item["claimed_at"] < now - timedelta(
            seconds=stale_seconds
        )
        if not (due or stale):
            return None
        token = uuid.uuid4().hex
        item.update(token=token, claimed_at=now, due=now + timedelta(seconds=stale_seconds))
        item["record"] = replace(rec, state=ReportState.IN_PROGRESS, attempts=rec.attempts + 1)
        return ReportClaim(rec, rec.attempts + 1, token)

    async def finish(
        self,
        report_id: str,
        token: str,
        state: ReportState,
        *,
        next_retry_at: datetime | None = None,
        error: str | None = None,
        comment_id: str | None = None,
    ) -> bool:
        item = self.items[report_id]
        rec: ReportRecord = item["record"]
        if rec.state is not ReportState.IN_PROGRESS or item["token"] != token:
            return False
        settled = state is not ReportState.PENDING
        item["record"] = replace(
            rec, state=state, last_error=error, comment_id=comment_id or rec.comment_id,
            next_retry_at=None if settled else (next_retry_at or self.now()),
            updated_at=self.now(),
        )  # fmt: skip
        item.update(token=None, due=None if settled else (next_retry_at or self.now()))
        return True

    async def due(self, limit: int) -> list[str]:
        now = self.now()
        ready = [(i["due"], k) for k, i in self.items.items() if i["due"] is not None]
        return [k for due, k in sorted(ready) if due <= now][:limit]

    async def get(self, report_id: str) -> ReportRecord | None:
        item = self.items.get(report_id)
        return item["record"] if item else None

    async def recent(self, limit: int) -> list[ReportRecord]:
        recs = [i["record"] for i in self.items.values()]
        return sorted(recs, key=lambda r: r.created_at or self.now(), reverse=True)[:limit]
