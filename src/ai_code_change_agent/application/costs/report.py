"""Terminal cost reports, built only from persisted usage records (a restart loses nothing).

The amounts are LLM API cost only: no Docker, database, source host, issue tracker or server
cost."""

import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from ai_code_change_agent.application.costs.usage import (
    STAGE_CORRECTION,
    STAGE_IMPLEMENTATION,
    STAGE_PLANNING,
    LlmCallRecord,
)

RULE = "=" * 56
THIN = "-" * 56
STAGE_ORDER = (STAGE_PLANNING, STAGE_IMPLEMENTATION, STAGE_CORRECTION)
STAGE_LABEL = {
    STAGE_PLANNING: "Planning (investigation + plan)",
    STAGE_IMPLEMENTATION: "Implementation (incl. tests, final report)",
    STAGE_CORRECTION: "Correction",
}
FAILED_STATES = frozenset({"FAILED", "BLOCKED", "REJECTED", "CANCELLED"})


@dataclass(frozen=True)
class CostRow:
    record: LlmCallRecord
    issue_key: str | None
    attempt_number: int | None
    attempt_status: str | None
    repository: str | None


class CostQuery(Protocol):
    async def rows(
        self,
        *,
        attempt_ids: Sequence[uuid.UUID] | None = None,
        issue_keys: Sequence[str] | None = None,
        session_id: uuid.UUID | None = None,
    ) -> list[CostRow]: ...


@dataclass(frozen=True)
class Totals:
    calls: int
    known_calls: int  # calls whose usage and price are both known
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost: Decimal  # sum of the known calls only
    models: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.calls == self.known_calls

    def money(self) -> str:
        text = f"${self.cost:.4f}"
        if self.calls and not self.complete:
            missing = self.calls - self.known_calls
            return f"at least {text} (INCOMPLETE: {missing} call(s) without usage)"
        return text


def totals(records: Iterable[LlmCallRecord]) -> Totals:
    recs = list(records)
    known = [r for r in recs if r.usage is not None and r.cost_usd is not None]
    usages = [r.usage for r in recs if r.usage is not None]
    return Totals(
        calls=len(recs),
        known_calls=len(known),
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        cache_write_tokens=sum(u.cache_write_tokens for u in usages),
        cache_read_tokens=sum(u.cache_read_tokens for u in usages),
        cost=sum((r.cost_usd for r in known if r.cost_usd is not None), Decimal(0)),
        models=tuple(sorted({r.model for r in recs})),
    )


def _line(label: str, value: str) -> str:
    return f"{label:<17}: {value}"


def task_report(
    attempt_id: uuid.UUID,
    rows: Sequence[CostRow],
    *,
    provider: str,
    primary_model: str,
    pricing_version: str,
    result: str | None = None,
) -> str:
    """One attempt, with the task's cumulative cost over all of its attempts."""
    mine = [r for r in rows if r.record.attempt_id == attempt_id]
    head = mine[0] if mine else None
    issue = head.issue_key if head else None
    t = totals(r.record for r in mine)
    lines = [
        RULE,
        "AI CODE CHANGE AGENT - TASK COST REPORT (LLM API cost only)",
        RULE,
        _line("Jira Issue", issue or "-"),
        _line("Attempt", f"{head.attempt_number} ({attempt_id})" if head else str(attempt_id)),
        _line("Repository", (head.repository if head else None) or "-"),
        _line("Provider", provider),
        _line("Primary Model", primary_model),
        _line("Models Used", ", ".join(t.models) or "-"),
        _line("Workflow Result", result or (head.attempt_status if head else None) or "-"),
        "",
        "Token Usage",
        THIN,
        _line("Model Calls", f"{t.calls:,}"),
        _line("Input Tokens", f"{t.input_tokens:,}"),
        _line("Output Tokens", f"{t.output_tokens:,}"),
        _line("Cache Write", f"{t.cache_write_tokens:,}"),
        _line("Cache Read", f"{t.cache_read_tokens:,}"),
        "",
        "Cost Breakdown by Stage",
        THIN,
    ]
    by_stage: dict[str, list[LlmCallRecord]] = defaultdict(list)
    for r in mine:
        by_stage[r.record.stage].append(r.record)
    stages = [s for s in STAGE_ORDER if s in by_stage] + sorted(set(by_stage) - set(STAGE_ORDER))
    if not stages:
        lines.append("(no model calls recorded for this attempt)")
    for stage in stages:
        st = totals(by_stage[stage])
        lines.append(
            f"{STAGE_LABEL.get(stage, stage)}: {st.money()}  "
            f"[{st.calls} calls, in {st.input_tokens:,} / out {st.output_tokens:,}, "
            f"{', '.join(st.models)}]"
        )
    lines += ["", _line("Attempt LLM Cost", f"{t.money()} USD")]
    if issue is not None:
        task_rows = [r for r in rows if r.issue_key == issue]
        tt = totals(r.record for r in task_rows)
        attempts = sorted({r.attempt_number for r in task_rows if r.attempt_number is not None})
        lines.append(
            _line("Task LLM Cost", f"{tt.money()} USD over {len(attempts)} attempt(s) with usage")
        )
        repos = sorted({r.repository for r in task_rows if r.repository})
        if len(repos) > 1:  # a multi-repository Story: cost per repository
            for repo in repos:
                rt = totals(r.record for r in task_rows if r.repository == repo)
                lines.append(_line(f"  {repo}"[:17], f"{rt.money()} USD"))
    lines += [_line("Pricing", pricing_version), RULE]
    return "\n".join(lines)


def session_summary(
    session_id: uuid.UUID,
    session_rows: Sequence[CostRow],
    task_rows: Sequence[CostRow],
    *,
    pricing_version: str,
) -> str:
    """Every task this worker session spent money on, including failed attempts.

    Session cost counts only the calls made in this session; task cost is cumulative over all of
    the task's attempts (across sessions), each call counted once."""
    issues = sorted({r.issue_key or "(no task)" for r in session_rows})
    attempts = {r.record.attempt_id for r in session_rows if r.record.attempt_id}
    latest: dict[str, tuple[int, str]] = {}
    for r in session_rows:
        key = r.issue_key or "(no task)"
        num = r.attempt_number or 0
        if key not in latest or num > latest[key][0]:
            latest[key] = (num, r.attempt_status or "-")
    completed = sum(1 for k in issues if latest.get(k, (0, ""))[1] == "COMPLETED")
    failed = sum(1 for k in issues if latest.get(k, (0, ""))[1] in FAILED_STATES)
    lines = [
        RULE,
        "EXECUTION SESSION COST SUMMARY (LLM API cost only)",
        RULE,
        _line("Session", str(session_id)),
        _line("Completed Tasks", str(completed)),
        _line("Failed Tasks", str(failed)),
        _line("Other Tasks", str(len(issues) - completed - failed)),
        _line("Total Attempts", str(len(attempts))),
        "",
        f"{'Task':<10}{'Status':<12}{'Model(s)':<20}{'Session':>14}{'Task total':>14}",
        THIN,
    ]
    for issue in issues:
        mine = totals(r.record for r in session_rows if (r.issue_key or "(no task)") == issue)
        whole = totals(r.record for r in task_rows if (r.issue_key or "(no task)") == issue)
        flag = "" if mine.complete and whole.complete else " *"
        lines.append(
            f"{issue:<10}{latest[issue][1]:<12}{', '.join(mine.models)[:19]:<20}"
            f"{f'${mine.cost:.4f}':>14}{f'${whole.cost:.4f}':>14}{flag}"
        )
        for num in sorted(
            {r.attempt_number for r in session_rows if r.issue_key == issue and r.attempt_number}
        ):
            at = totals(
                r.record for r in session_rows if r.issue_key == issue and r.attempt_number == num
            )
            lines.append(
                f"  attempt {num:<5}{'':<17}{f'{at.calls} calls':<20}{f'${at.cost:.4f}':>14}"
            )
    total = totals(r.record for r in session_rows)
    lines += [THIN, _line("Session LLM Cost", f"{total.money()} USD")]
    if not total.complete:
        lines.append("* usage was missing for some calls; their cost is unknown, not zero")
    lines += [_line("Pricing", pricing_version), RULE]
    return "\n".join(lines)
