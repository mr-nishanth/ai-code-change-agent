"""Normalised, immutable Jira models.

Everything in these models originates from Jira users and is UNTRUSTED DATA: descriptions,
comments, custom fields, labels and display names may contain prompt-injection text. Consumers
must never treat it as instructions. Fields Jira did not return are None/empty, never invented.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class JiraProject(_Model):
    id: str | None = None
    key: str | None = None
    name: str | None = None


class JiraUser(_Model):
    """Only account id, display name and active flag are kept; e-mail addresses are dropped."""

    account_id: str | None = None
    display_name: str | None = None
    active: bool | None = None


class JiraRichText(_Model):
    """Atlassian Document Format (or plain text) with a safe plain-text rendering.

    `structure` keeps the original ADF document; `unsupported_nodes` lists node types or limits
    the renderer could not interpret, so nothing is dropped silently.
    """

    text: str
    structure: dict[str, Any] | None = None
    unsupported_nodes: tuple[str, ...] = ()


CustomFieldKind = Literal[
    "text", "number", "boolean", "option", "options", "user", "rich_text", "null", "unsupported"
]


class JiraCustomField(_Model):
    field_id: str
    kind: CustomFieldKind
    text: str | None = None
    values: tuple[str, ...] = ()
    rich_text: JiraRichText | None = None


class JiraIssue(_Model):
    id: str
    key: str
    summary: str | None = None
    description: JiraRichText | None = None
    issue_type: str | None = None
    project: JiraProject | None = None
    status: str | None = None
    priority: str | None = None
    labels: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    assignee: JiraUser | None = None
    reporter: JiraUser | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    custom_fields: dict[str, JiraCustomField] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class JiraComment(_Model):
    id: str
    author: JiraUser | None = None
    body: JiraRichText | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class JiraCommentList(_Model):
    items: tuple[JiraComment, ...] = ()
    total: int | None = None
    truncated: bool = False
    warnings: tuple[str, ...] = ()


class JiraStory(_Model):
    issue: JiraIssue
    comments: JiraCommentList | None = None
    comments_unavailable_reason: str | None = None
