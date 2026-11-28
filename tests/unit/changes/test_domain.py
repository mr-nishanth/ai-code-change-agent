"""Attempt state machine and hash-bound approvals (pure domain)."""

import uuid
from dataclasses import replace
from typing import Any

import pytest

from ai_code_change_agent.domain.changes.errors import (
    ApprovalRequiredError,
    ChangeValidationError,
    InvalidTransitionError,
    StaleApprovalError,
)
from ai_code_change_agent.domain.changes.models import (
    Approval,
    ApprovalDecision,
    ApprovalGate,
    CodeChangeAttempt,
)
from ai_code_change_agent.domain.changes.states import TERMINAL, TRANSITIONS, AttemptStatus

S = AttemptStatus
A, B = "a" * 64, "b" * 64
OP = "cli:operator"


def attempt(**kw: Any) -> CodeChangeAttempt:
    base = CodeChangeAttempt(uuid.uuid4(), uuid.uuid4(), 1, uuid.uuid4(), uuid.uuid4())
    return replace(base, **kw)


def test_every_state_has_a_transition_entry_and_terminals_are_final() -> None:
    assert set(TRANSITIONS) == set(S)
    assert all(TRANSITIONS[t] == frozenset() for t in TERMINAL)


def test_the_happy_path_needs_baseline_plan_approvals_and_manifest() -> None:
    a = attempt().transition(S.PREPARING)
    with pytest.raises(ChangeValidationError, match="baseline_not_pinned"):
        a.transition(S.PLANNING)
    a = a.pin_baseline("c" * 40).transition(S.PLANNING)
    with pytest.raises(ChangeValidationError, match="plan_missing"):
        a.transition(S.AWAITING_PLAN_APPROVAL)
    a = a.record_plan("art_x", A).transition(S.AWAITING_PLAN_APPROVAL)
    with pytest.raises(ApprovalRequiredError):
        a.transition(S.IMPLEMENTING)
    plan_ok = a.decide(ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, OP)
    a = a.transition(S.IMPLEMENTING, [plan_ok]).transition(S.VALIDATING)
    with pytest.raises(ChangeValidationError, match="manifest_missing"):
        a.transition(S.AWAITING_FINAL_APPROVAL)
    a = a.record_manifest("art_m", B).transition(S.AWAITING_FINAL_APPROVAL)
    with pytest.raises(ApprovalRequiredError):
        a.transition(S.DELIVERING, [plan_ok])
    final_ok = a.decide(ApprovalGate.FINAL, B, ApprovalDecision.APPROVED, OP)
    a = a.transition(S.DELIVERING, [plan_ok, final_ok]).transition(S.REPORTING)
    a = a.transition(S.COMPLETED)
    assert a.terminal and a.row_version > 1
    with pytest.raises(InvalidTransitionError):
        a.transition(S.FAILED, failure_code="x")


def test_an_approval_for_another_hash_is_refused_and_never_counts() -> None:
    a = attempt(status=S.AWAITING_PLAN_APPROVAL, plan_sha256=A, base_commit_sha="c" * 40)
    with pytest.raises(StaleApprovalError):
        a.decide(ApprovalGate.PLAN, B, ApprovalDecision.APPROVED, OP)
    forged = Approval(a.id, ApprovalGate.PLAN, B, ApprovalDecision.APPROVED, OP)
    assert a.valid_approval(ApprovalGate.PLAN, [forged]) is None
    with pytest.raises(ApprovalRequiredError):
        a.transition(S.IMPLEMENTING, [forged])


def test_changing_the_artifact_invalidates_the_earlier_approval() -> None:
    a = attempt(status=S.VALIDATING, manifest_sha256=A, base_commit_sha="c" * 40)
    a = a.transition(S.AWAITING_FINAL_APPROVAL)
    ok = a.decide(ApprovalGate.FINAL, A, ApprovalDecision.APPROVED, OP)
    assert a.valid_approval(ApprovalGate.FINAL, [ok]) is ok
    changed = replace(a, manifest_sha256=B)  # a new manifest (e.g. after a repair)
    assert changed.valid_approval(ApprovalGate.FINAL, [ok]) is None


def test_the_latest_decision_wins_and_a_rejection_is_not_an_approval() -> None:
    a = attempt(status=S.AWAITING_PLAN_APPROVAL, plan_sha256=A)
    yes = a.decide(ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, OP)
    no = a.decide(ApprovalGate.PLAN, A, ApprovalDecision.REJECTED, OP)
    assert a.valid_approval(ApprovalGate.PLAN, [yes, no]) is None


def test_decisions_only_at_the_waiting_gate_and_with_a_valid_actor() -> None:
    a = attempt(status=S.IMPLEMENTING, plan_sha256=A)
    with pytest.raises(InvalidTransitionError):
        a.decide(ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, OP)
    w = attempt(status=S.AWAITING_PLAN_APPROVAL, plan_sha256=A)
    with pytest.raises(ChangeValidationError):
        w.decide(ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, "not an actor")
    assert w.decide(ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, OP).actor_verified is False


def test_the_baseline_is_pinned_once() -> None:
    a = attempt(status=S.PREPARING).pin_baseline("c" * 40)
    assert a.pin_baseline("c" * 40).base_commit_sha == "c" * 40
    with pytest.raises(StaleApprovalError):
        a.pin_baseline("d" * 40)
    with pytest.raises(ChangeValidationError):
        attempt(status=S.PREPARING).pin_baseline("main")
    with pytest.raises(InvalidTransitionError):
        attempt(status=S.PLANNING).pin_baseline("c" * 40)


def test_repairs_are_bounded() -> None:
    a = attempt(status=S.VALIDATING)
    with pytest.raises(InvalidTransitionError, match="repair_budget_exhausted"):
        a.transition(S.IMPLEMENTING, max_repairs=0)
    once = a.transition(S.IMPLEMENTING, max_repairs=1)
    assert once.repair_count == 1
    with pytest.raises(InvalidTransitionError):
        replace(once, status=S.VALIDATING).transition(S.IMPLEMENTING, max_repairs=1)


def test_failure_and_blocked_need_a_code_and_artifacts_need_their_stage() -> None:
    with pytest.raises(ChangeValidationError):
        attempt(status=S.PREPARING).transition(S.BLOCKED)
    blocked = attempt(status=S.PREPARING).transition(S.BLOCKED, failure_code="needs_network")
    assert blocked.failure_code == "needs_network"
    with pytest.raises(InvalidTransitionError):
        attempt(status=S.IMPLEMENTING).record_plan("r", A)
    with pytest.raises(InvalidTransitionError):
        attempt(status=S.PLANNING).record_manifest("r", A)
    with pytest.raises(ChangeValidationError):
        attempt(status=S.PLANNING).record_plan("r", "nothex")
    with pytest.raises(ChangeValidationError):
        attempt(status=S.VALIDATING).record_manifest("r", "nothex")
    with pytest.raises(ChangeValidationError):
        Approval(uuid.uuid4(), ApprovalGate.PLAN, "x", ApprovalDecision.APPROVED, OP)


def test_a_rejection_may_carry_a_bounded_single_line_reason() -> None:
    a = attempt(status=S.AWAITING_PLAN_APPROVAL, plan_sha256=A, base_commit_sha="c" * 40)
    no = a.decide(ApprovalGate.PLAN, A, ApprovalDecision.REJECTED, OP, reason="wrong module")
    assert no.reason == "wrong module"
    assert a.decide(ApprovalGate.PLAN, A, ApprovalDecision.REJECTED, OP).reason is None
    for bad in ("", "two\nlines", "x" * 501, "c1\x9fx", "rtl\u202ex", "zw\u200dx", "\u2069"):
        with pytest.raises(ChangeValidationError, match="reason_invalid"):
            a.decide(ApprovalGate.PLAN, A, ApprovalDecision.REJECTED, OP, reason=bad)
    with pytest.raises(ChangeValidationError, match="reason_invalid"):  # approvals carry none
        a.decide(ApprovalGate.PLAN, A, ApprovalDecision.APPROVED, OP, reason="fine")
