"""Lines of model-written text for a Jira comment: one line, bounded, never a possible secret.

Stored plans are scanned as JSON, where escaping can hide a secret from the detector (an escaped
quote ends an assigned value early); a comment holds the decoded text, so it is scanned again."""

from ai_code_change_agent.application.artifacts.redaction import find_secrets

WITHHELD = "[withheld: possible secret]"


def safe_line(text: str, limit: int) -> str:
    """`text` on one line, cut to `limit` characters; WITHHELD when the platform's secret detector
    flags it. The whole text is scanned before the cut, so a cut never hides a secret."""
    line = " ".join(text.split())
    return WITHHELD if find_secrets(text) or find_secrets(line) else line[:limit]
