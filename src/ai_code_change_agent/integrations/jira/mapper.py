"""Pure mapping of Jira REST JSON to application models. Never trusts the payload's shape."""

import re
from datetime import UTC, datetime
from typing import Any

from ai_code_change_agent.application.jira.errors import JiraResponseError
from ai_code_change_agent.application.jira.models import (
    JiraComment,
    JiraCustomField,
    JiraIssue,
    JiraProject,
    JiraRichText,
    JiraUser,
)
from ai_code_change_agent.integrations.jira.adf import to_rich_text

_OFFSET_WITHOUT_COLON = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _OFFSET_WITHOUT_COLON.sub(r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _name(value: object) -> str | None:
    return _str(value.get("name")) if isinstance(value, dict) else None


def map_user(value: object) -> JiraUser | None:
    if not isinstance(value, dict):
        return None
    active = value.get("active")
    return JiraUser(
        account_id=_str(value.get("accountId")),
        display_name=_str(value.get("displayName")),
        active=active if isinstance(active, bool) else None,
    )


def map_project(value: object) -> JiraProject | None:
    if not isinstance(value, dict):
        return None
    return JiraProject(
        id=_str(value.get("id")), key=_str(value.get("key")), name=_str(value.get("name"))
    )


def _scalar(value: object) -> str | None:
    """String form of a list member, or None when the member is not understood."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        for key in ("value", "name", "displayName"):
            found = _str(value.get(key))
            if found:
                return found
    return None


def _custom_field(field_id: str, value: object) -> JiraCustomField:
    if value is None:
        return JiraCustomField(field_id=field_id, kind="null")
    if isinstance(value, bool):
        return JiraCustomField(field_id=field_id, kind="boolean", text=str(value).lower())
    if isinstance(value, int | float):
        return JiraCustomField(field_id=field_id, kind="number", text=str(value))
    if isinstance(value, str):
        return JiraCustomField(field_id=field_id, kind="text", text=value)
    if isinstance(value, dict):
        if value.get("type") == "doc":
            rich = to_rich_text(value)
            return JiraCustomField(
                field_id=field_id,
                kind="rich_text",
                text=rich.text if rich else None,
                rich_text=rich,
            )
        if "accountId" in value:
            user = map_user(value)
            label = (user.display_name or user.account_id) if user else None
            return JiraCustomField(field_id=field_id, kind="user", text=label)
        label = _str(value.get("value")) or _str(value.get("name"))
        if label:
            child = value.get("child")
            child_label = _str(child.get("value")) if isinstance(child, dict) else None
            text = f"{label} / {child_label}" if child_label else label
            return JiraCustomField(field_id=field_id, kind="option", text=text)
    if isinstance(value, list):
        members = [_scalar(member) for member in value]
        if all(m is not None for m in members):
            return JiraCustomField(
                field_id=field_id, kind="options", values=tuple(m for m in members if m is not None)
            )
    return JiraCustomField(field_id=field_id, kind="unsupported")


def map_issue(raw: object, field_map: dict[str, str]) -> JiraIssue:
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("id"), str)
        or not isinstance(raw.get("key"), str)
    ):
        raise JiraResponseError("malformed_issue", operation="get_issue")
    fields = raw.get("fields")
    fields = fields if isinstance(fields, dict) else {}
    warnings: list[str] = []

    raw_labels = fields.get("labels")
    labels: list[str] = []
    if isinstance(raw_labels, list):
        for label in raw_labels:
            if isinstance(label, str):
                labels.append(label)
            elif "label_invalid" not in warnings:
                warnings.append("label_invalid")

    raw_components = fields.get("components")
    components = (
        tuple(n for n in (_name(c) for c in raw_components) if n)
        if isinstance(raw_components, list)
        else ()
    )

    timestamps: dict[str, datetime | None] = {}
    for name in ("created", "updated"):
        parsed = parse_timestamp(fields.get(name))
        if parsed is None and fields.get(name) is not None:
            warnings.append(f"timestamp_invalid:{name}")
        timestamps[name] = parsed

    custom: dict[str, JiraCustomField] = {}
    for logical, field_id in field_map.items():
        if field_id not in fields:
            warnings.append(f"custom_field_absent:{logical}")
            continue
        mapped = _custom_field(field_id, fields[field_id])
        if mapped.kind == "unsupported":
            warnings.append(f"custom_field_unsupported:{logical}")
        custom[logical] = mapped

    description: JiraRichText | None = to_rich_text(fields.get("description"))
    return JiraIssue(
        id=raw["id"],
        key=raw["key"],
        summary=_str(fields.get("summary")),
        description=description,
        issue_type=_name(fields.get("issuetype")),
        project=map_project(fields.get("project")),
        status=_name(fields.get("status")),
        priority=_name(fields.get("priority")),
        labels=tuple(labels),
        components=components,
        assignee=map_user(fields.get("assignee")),
        reporter=map_user(fields.get("reporter")),
        created_at=timestamps["created"],
        updated_at=timestamps["updated"],
        custom_fields=custom,
        warnings=tuple(warnings),
    )


def map_comment(raw: object) -> JiraComment:
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
        raise JiraResponseError("malformed_comment", operation="get_comments")
    return JiraComment(
        id=raw["id"],
        author=map_user(raw.get("author")),
        body=to_rich_text(raw.get("body")),
        created_at=parse_timestamp(raw.get("created")),
        updated_at=parse_timestamp(raw.get("updated")),
    )


def comment_page_items(raw: dict[str, Any]) -> tuple[list[Any], int | None]:
    comments = raw.get("comments")
    total = raw.get("total")
    if not isinstance(comments, list) or (
        total is not None and (not isinstance(total, int) or isinstance(total, bool))
    ):
        raise JiraResponseError("malformed_comments_page", operation="get_comments")
    return comments, total
