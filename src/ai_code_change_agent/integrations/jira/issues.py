"""Jira Cloud implementation of the read-only `JiraIssueReader` port."""

import re
from urllib.parse import quote

from ai_code_change_agent.application.jira.errors import (
    JiraInvalidIssueReferenceError,
    JiraResponseError,
)
from ai_code_change_agent.application.jira.models import (
    JiraComment,
    JiraCommentList,
    JiraIssue,
)
from ai_code_change_agent.integrations.jira.client import JiraHttpClient
from ai_code_change_agent.integrations.jira.mapper import comment_page_items, map_comment, map_issue
from ai_code_change_agent.integrations.jira.pagination import Page, collect_pages

# Canonical upper-case issue key (e.g. ABC-123) or a numeric issue id. Anything else is refused
# before a URL is built, so a reference can never alter the request path or query.
_ISSUE_REF = re.compile(r"[A-Z][A-Z0-9_]{1,31}-[1-9][0-9]{0,9}|[1-9][0-9]{0,18}")

BASE_FIELDS = (
    "summary",
    "description",
    "issuetype",
    "project",
    "status",
    "priority",
    "labels",
    "components",
    "assignee",
    "reporter",
    "created",
    "updated",
)


class JiraCloudIssueReader:
    def __init__(
        self,
        client: JiraHttpClient,
        *,
        field_map: dict[str, str],
        comment_page_size: int,
        max_comments: int,
    ) -> None:
        self._client = client
        self._field_map = dict(field_map)
        self._page_size = comment_page_size
        self._max_comments = max_comments

    @property
    def field_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self._field_map.values())))

    @property
    def is_closed(self) -> bool:
        return self._client.is_closed

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _path(issue_ref: str, suffix: str = "") -> str:
        if not _ISSUE_REF.fullmatch(issue_ref):
            raise JiraInvalidIssueReferenceError()
        return f"/rest/api/3/issue/{quote(issue_ref, safe='')}{suffix}"

    async def get_issue(self, issue_ref: str) -> JiraIssue:
        path = self._path(issue_ref)
        raw = await self._client.get_json(
            path,
            params={"fields": ",".join((*BASE_FIELDS, *self.field_ids))},
            operation="get_issue",
        )
        return map_issue(raw, self._field_map)

    async def get_comments(self, issue_ref: str) -> JiraCommentList:
        path = self._path(issue_ref, "/comment")

        async def fetch(start: int, size: int) -> Page[object]:
            raw = await self._client.get_json(
                path,
                params={"startAt": start, "maxResults": size, "orderBy": "created"},
                operation="get_comments",
            )
            items, total = comment_page_items(raw)
            return Page(items=list(items), total=total)

        collected, total, truncated = await collect_pages(
            fetch, page_size=self._page_size, max_items=self._max_comments
        )
        comments: list[JiraComment] = []
        warnings: list[str] = []
        for index, raw_comment in enumerate(collected):
            try:
                comments.append(map_comment(raw_comment))
            except JiraResponseError:
                warnings.append(f"comment_malformed:{index}")
        return JiraCommentList(
            items=tuple(comments), total=total, truncated=truncated, warnings=tuple(warnings)
        )
