"""Deterministic branch names and commit messages derived from verified identifiers only."""

import re

BRANCH = re.compile(r"aica/[a-z0-9][a-z0-9_-]{0,60}-a[0-9]{1,4}-[0-9a-f]{12}")


def branch_name(issue_key: str, attempt_number: int, changeset_sha256: str) -> str:
    name = f"aica/{issue_key.lower()}-a{attempt_number}-{changeset_sha256[:12]}"
    if not BRANCH.fullmatch(name):
        msg = "branch_name_invalid"
        raise ValueError(msg)
    return name


def commit_message(
    *,
    issue_key: str,
    attempt_number: int,
    base_commit_sha: str,
    plan_sha256: str,
    changeset_sha256: str,
    validation_report_sha256: str,
) -> str:
    """No text written by a model or a Story: identifiers and hashes only."""
    return (
        f"AI Code Change: {issue_key} (attempt {attempt_number})\n\n"
        f"Base-Commit: {base_commit_sha}\n"
        f"Plan-SHA256: {plan_sha256}\n"
        f"Changeset-SHA256: {changeset_sha256}\n"
        f"Validation-Report-SHA256: {validation_report_sha256}\n"
    )
