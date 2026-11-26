"""Secret detection over changed content (the platform's shared detector)."""

from collections.abc import Sequence

from ai_code_change_agent.application.artifacts.redaction import find_secrets
from ai_code_change_agent.application.workspaces.models import FileChange
from ai_code_change_agent.validation.schemas import CheckKind, CheckRecord, CheckStatus


def secrets(changes: Sequence[FileChange]) -> CheckRecord:
    hits = [
        c.path
        for c in changes
        if c.content is not None and find_secrets(c.content.decode("utf-8", errors="replace"))
    ]
    if hits:
        return CheckRecord(
            kind=CheckKind.SECRETS,
            status=CheckStatus.FAILED,
            detail=f"possible secrets in: {', '.join(sorted(hits)[:10])}",
        )
    return CheckRecord(kind=CheckKind.SECRETS, status=CheckStatus.PASSED)
