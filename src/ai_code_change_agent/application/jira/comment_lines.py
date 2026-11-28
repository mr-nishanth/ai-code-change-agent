"""Lines of model-written text for a Jira comment: one line, bounded, never a possible secret.

Stored plans are scanned as JSON, where escaping can hide a secret from the detector (an escaped
quote ends an assigned value early); a comment holds the decoded text, so it is scanned again."""

from ai_code_change_agent.agents.coding.schemas import ImplementationPlan
from ai_code_change_agent.application.artifacts.redaction import find_secrets
from ai_code_change_agent.validation.schemas import ValidationReport

WITHHELD = "[withheld: possible secret]"


def safe_line(text: str, limit: int) -> str:
    """`text` on one line, cut to `limit` characters; WITHHELD when the platform's secret detector
    flags it. The whole text is scanned before the cut, so a cut never hides a secret."""
    line = " ".join(text.split())
    return WITHHELD if find_secrets(text) or find_secrets(line) else line[:limit]


def clarification_detail(plan: ImplementationPlan) -> str:
    """The planner's summary and its questions for a Story that needs clarification (bounded)."""
    questions = "".join(f"- {safe_line(q, 300)}\n" for q in plan.clarification_questions[:10])
    return (
        f"Summary: {safe_line(plan.summary, 300)}\n"
        f"Questions to answer in the Story before a new attempt:\n{questions}"
    )


def validation_detail(report: ValidationReport) -> str:
    """Why independent validation failed: failed checks with their fixed detail and the commands
    that did not pass (never file content or command output). Empty when nothing failed."""
    lines = [
        f"{c.kind.value}: {safe_line(c.detail or c.status.value, 300)}"
        for c in report.checks
        if c.status.value == "FAILED"
    ]
    lines += [
        f"command {c.command_id}: {c.status}" + (f" ({c.reason})" if c.reason else "")
        for c in report.commands
        if c.status != "PASSED"
    ]
    if not lines:
        return ""
    return "Validation detail:\n" + "".join(f"- {safe_line(ln, 320)}\n" for ln in lines[:8])
