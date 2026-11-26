"""PR eligibility: the only place that turns checks into PASSED or FAILED. The agent's own report
is never an input."""

from collections.abc import Sequence

from ai_code_change_agent.validation.schemas import (
    CheckKind,
    CheckRecord,
    CheckStatus,
    CommandRecord,
    Decision,
)


def required_tests(commands: Sequence[CommandRecord]) -> CheckRecord:
    tests = [c for c in commands if c.kind == "test"]
    if not tests:
        return CheckRecord(
            kind=CheckKind.REQUIRED_TESTS,
            status=CheckStatus.FAILED,
            detail="profile_defines_no_test_command",
        )
    failed = [c.command_id for c in tests if c.status != "PASSED"]
    return CheckRecord(
        kind=CheckKind.REQUIRED_TESTS,
        status=CheckStatus.FAILED if failed else CheckStatus.PASSED,
        detail=f"tests did not pass: {', '.join(failed)}" if failed else None,
    )


def commands_check(commands: Sequence[CommandRecord], policy: str) -> CheckRecord:
    """Every configured validation command must pass. Under the strict policy a command that
    could not run (blocked, timed out, error) is a failure too; standard treats it the same way
    because a check that did not run proves nothing."""
    bad = [f"{c.command_id}={c.status}" for c in commands if c.status != "PASSED"]
    return CheckRecord(
        kind=CheckKind.COMMANDS,
        status=CheckStatus.FAILED if bad else CheckStatus.PASSED,
        detail=("; ".join(bad) + f" (policy {policy})")[:500] if bad else None,
    )


def decide(checks: Sequence[CheckRecord]) -> tuple[Decision, list[str]]:
    reasons = [
        f"{c.kind.value}: {c.detail or 'failed'}"
        for c in checks
        if c.mandatory and c.status in (CheckStatus.FAILED, CheckStatus.SKIPPED)
    ]
    return (Decision.FAILED if reasons else Decision.PASSED), reasons
