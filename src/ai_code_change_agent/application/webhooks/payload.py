"""Strict parsing of Jira Cloud webhook JSON. Runs only AFTER signature verification."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from ai_code_change_agent.domain.webhooks.models import IssueRef, RejectionCode, WebhookRejected


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    webhook_event: StrictStr = Field(alias="webhookEvent", min_length=1, max_length=100)


class _Project(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: StrictStr = Field(min_length=1, max_length=64)


class _IssueType(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: StrictStr = Field(min_length=1, max_length=64)


class _Fields(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project: _Project
    issuetype: _IssueType


class _Issue(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: StrictStr = Field(pattern=r"^[0-9]{1,20}$")
    key: StrictStr = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}-[0-9]{1,10}$")
    fields: _Fields


class _IssueEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    issue: _Issue


def _invalid() -> WebhookRejected:
    return WebhookRejected(RejectionCode.PAYLOAD_INVALID)


def load_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise _invalid() from None


def envelope_event(data: Any) -> str:
    try:
        return _Envelope.model_validate(data).webhook_event
    except ValidationError:
        raise _invalid() from None


def issue_ref(data: Any) -> IssueRef:
    try:
        issue = _IssueEvent.model_validate(data).issue
    except ValidationError:
        raise _invalid() from None
    return IssueRef(
        issue_id=issue.id,
        issue_key=issue.key,
        project_key=issue.fields.project.key,
        issue_type=issue.fields.issuetype.name,
    )
