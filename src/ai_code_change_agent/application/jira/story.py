"""Story extraction: the parts of a Jira issue the workflow uses. Everything here is untrusted
text and is only ever shown to the coding agent inside marked data blocks."""

import re
from dataclasses import dataclass

from ai_code_change_agent.application.jira.models import JiraIssue

_HEADING = re.compile(r"^\s*(?:#+\s*|h\d\.\s*)?acceptance\s+criteria\s*:?\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$")
MAX_TEXT = 20_000


@dataclass(frozen=True)
class Story:
    issue_key: str
    summary: str
    description: str | None
    acceptance_criteria: tuple[str, ...]


def acceptance_criteria(text: str | None) -> tuple[str, ...]:
    """Bullet items following an "Acceptance criteria" heading, until the next blank-line gap
    followed by a non-bullet line."""
    if not text:
        return ()
    items: list[str] = []
    inside = False
    for line in text.splitlines():
        if _HEADING.match(line):
            inside = True
            continue
        if not inside:
            continue
        bullet = _BULLET.match(line)
        if bullet:
            items.append(bullet.group(1)[:500])
        elif line.strip() and items:
            break
    return tuple(items[:50])


def story_of(issue: JiraIssue) -> Story:
    description = issue.description.text if issue.description else None
    if description is not None:
        description = description[:MAX_TEXT]
    return Story(
        issue_key=issue.key,
        summary=(issue.summary or "")[:500],
        description=description,
        acceptance_criteria=acceptance_criteria(description),
    )
