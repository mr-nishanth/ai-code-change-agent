"""Attempt lifecycle for the one Deep Agent workflow (two controlled phases, two human gates).

PENDING -> PREPARING -> PLANNING -> AWAITING_PLAN_APPROVAL -> IMPLEMENTING -> VALIDATING
        -> AWAITING_FINAL_APPROVAL -> DELIVERING -> REPORTING -> COMPLETED

VALIDATING may return to IMPLEMENTING for a bounded repair. BLOCKED means the attempt could not be
prepared safely (for example dependencies need network access); REJECTED means an operator
rejected the plan or the final artifact. Terminal attempts never change again.
"""

from enum import StrEnum


class AttemptStatus(StrEnum):
    PENDING = "PENDING"
    PREPARING = "PREPARING"
    PLANNING = "PLANNING"
    AWAITING_PLAN_APPROVAL = "AWAITING_PLAN_APPROVAL"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    AWAITING_FINAL_APPROVAL = "AWAITING_FINAL_APPROVAL"
    DELIVERING = "DELIVERING"
    REPORTING = "REPORTING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


S = AttemptStatus
TERMINAL: frozenset[AttemptStatus] = frozenset(
    {S.COMPLETED, S.BLOCKED, S.FAILED, S.CANCELLED, S.REJECTED}
)
_STOP = {S.FAILED, S.CANCELLED}
TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    S.PENDING: frozenset({S.PREPARING, *_STOP}),
    S.PREPARING: frozenset({S.PLANNING, S.BLOCKED, *_STOP}),
    S.PLANNING: frozenset({S.AWAITING_PLAN_APPROVAL, *_STOP}),
    S.AWAITING_PLAN_APPROVAL: frozenset({S.IMPLEMENTING, S.REJECTED, S.CANCELLED}),
    S.IMPLEMENTING: frozenset({S.VALIDATING, *_STOP}),
    S.VALIDATING: frozenset({S.AWAITING_FINAL_APPROVAL, S.IMPLEMENTING, *_STOP}),
    S.AWAITING_FINAL_APPROVAL: frozenset({S.DELIVERING, S.REJECTED, S.CANCELLED}),
    S.DELIVERING: frozenset({S.REPORTING, *_STOP}),
    S.REPORTING: frozenset({S.COMPLETED}),
    **{t: frozenset() for t in TERMINAL},
}


def can_transition(source: AttemptStatus, target: AttemptStatus) -> bool:
    return target in TRANSITIONS[source]
