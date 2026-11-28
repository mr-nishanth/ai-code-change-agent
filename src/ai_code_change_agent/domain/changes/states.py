"""Attempt lifecycle for the one Deep Agent workflow (two controlled phases, two human gates).

PENDING -> PREPARING -> PLANNING -> AWAITING_PLAN_APPROVAL -> IMPLEMENTING -> VALIDATING
        -> AWAITING_FINAL_APPROVAL -> DELIVERING -> REPORTING -> COMPLETED

VALIDATING may return to IMPLEMENTING for a bounded repair. BLOCKED means the attempt could not be
prepared safely (for example dependencies need network access); REJECTED means an operator
rejected the plan or the final artifact. Terminal attempts never change again.

A request's status projects its current attempt's status. Before any attempt it is RECEIVED, or
BLOCKED when no attempt will start from its intake (intake refused it, or its job ended DEAD by any
path, including a worker that died on the last attempt: `lease_expired_attempts_exhausted`; intake
is never re-run, so BLOCKED is terminal there too). An attempt a DEAD job leaves behind becomes
FAILED (`job_attempts_exhausted`), see `orchestration.dead_jobs`.
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
    # FAILED from a gate: the workflow checkpoint expired before the approval (checkpoint_expired)
    S.AWAITING_PLAN_APPROVAL: frozenset({S.IMPLEMENTING, S.REJECTED, *_STOP}),
    S.IMPLEMENTING: frozenset({S.VALIDATING, *_STOP}),
    S.VALIDATING: frozenset({S.AWAITING_FINAL_APPROVAL, S.IMPLEMENTING, *_STOP}),
    S.AWAITING_FINAL_APPROVAL: frozenset({S.DELIVERING, S.REJECTED, *_STOP}),
    S.DELIVERING: frozenset({S.REPORTING, *_STOP}),
    S.REPORTING: frozenset({S.COMPLETED}),
    **{t: frozenset() for t in TERMINAL},
}


def can_transition(source: AttemptStatus, target: AttemptStatus) -> bool:
    return target in TRANSITIONS[source]
