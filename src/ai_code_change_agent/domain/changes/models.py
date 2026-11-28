"""Code change requests, attempts and hash-bound approvals. Pure Python, no framework imports.

Safety guarantees kept from the previous platform:
- one Jira Story is one request; duplicate deliveries never create another request or attempt;
- one non-terminal attempt per request at a time;
- status changes only through `CodeChangeAttempt.transition`;
- an approval binds a gate to the exact artifact hash; recording a different plan or manifest
  makes every earlier approval for that gate invalid (it no longer matches);
- the baseline commit is pinned once and never changes;
- approvals are recorded with `actor_verified=False` until operator SSO exists.
"""

import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum

from ai_code_change_agent.domain.changes.errors import (
    ApprovalRequiredError,
    ChangeValidationError,
    InvalidTransitionError,
    StaleApprovalError,
)
from ai_code_change_agent.domain.changes.states import TERMINAL, AttemptStatus, can_transition

SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
ACTOR = re.compile(r"[a-z]{2,16}:[A-Za-z0-9_.@-]{1,64}")
REASON_MAX = 500
REASON = re.compile(rf"[^\x00-\x1f\x7f]{{1,{REASON_MAX}}}")  # one line, no control characters
S = AttemptStatus


class ApprovalGate(StrEnum):
    PLAN = "PLAN"
    FINAL = "FINAL"


class ApprovalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class Approval:
    attempt_id: uuid.UUID
    gate: ApprovalGate
    artifact_sha256: str
    decision: ApprovalDecision
    actor: str
    actor_verified: bool = False
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None  # an operator's optional reason for a rejection (one line)

    def __post_init__(self) -> None:
        if not SHA256.fullmatch(self.artifact_sha256):
            raise ChangeValidationError("approval_hash_invalid")
        if not ACTOR.fullmatch(self.actor):
            raise ChangeValidationError("actor_invalid")
        if self.reason is not None and (
            self.decision is not ApprovalDecision.REJECTED or not REASON.fullmatch(self.reason)
        ):
            raise ChangeValidationError("reason_invalid")


@dataclass(frozen=True)
class CodeChangeRequest:
    id: uuid.UUID
    jira_issue_id: str
    jira_issue_key: str
    jira_project_key: str
    repository_id: uuid.UUID | None = None
    profile_version_id: uuid.UUID | None = None
    current_attempt_id: uuid.UUID | None = None
    status: str = "RECEIVED"  # projection of the current attempt


@dataclass(frozen=True)
class CodeChangeAttempt:
    id: uuid.UUID
    request_id: uuid.UUID
    number: int
    repository_id: uuid.UUID
    profile_version_id: uuid.UUID
    status: AttemptStatus = S.PENDING
    base_commit_sha: str | None = None
    plan_ref: str | None = None
    plan_sha256: str | None = None
    manifest_ref: str | None = None
    manifest_sha256: str | None = None
    repair_count: int = 0
    failure_code: str | None = None
    row_version: int = 1

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    def _artifact(self, gate: ApprovalGate) -> str | None:
        return self.plan_sha256 if gate is ApprovalGate.PLAN else self.manifest_sha256

    def valid_approval(self, gate: ApprovalGate, approvals: list[Approval]) -> Approval | None:
        """The latest decision for this gate on the *current* artifact, if it is an approval."""
        current = self._artifact(gate)
        matching = [
            a
            for a in approvals
            if a.attempt_id == self.id and a.gate is gate and a.artifact_sha256 == current
        ]
        if current is None or not matching:
            return None
        latest = max(matching, key=lambda a: a.decided_at)
        return latest if latest.decision is ApprovalDecision.APPROVED else None

    def transition(
        self,
        target: AttemptStatus,
        approvals: list[Approval] = (),  # type: ignore[assignment]
        *,
        max_repairs: int = 0,
        failure_code: str | None = None,
    ) -> "CodeChangeAttempt":
        if not can_transition(self.status, target):
            raise InvalidTransitionError(f"{self.status.value}->{target.value}")
        repairs = self.repair_count
        if self.status is S.PREPARING and target is S.PLANNING and self.base_commit_sha is None:
            raise ChangeValidationError("baseline_not_pinned")
        if (
            self.status is S.PLANNING
            and target is S.AWAITING_PLAN_APPROVAL
            and not self.plan_sha256
        ):
            raise ChangeValidationError("plan_missing")
        if (
            target is S.IMPLEMENTING
            and self.status is S.AWAITING_PLAN_APPROVAL
            and self.valid_approval(ApprovalGate.PLAN, list(approvals)) is None
        ):
            raise ApprovalRequiredError("plan_approval_required")
        if target is S.IMPLEMENTING and self.status is S.VALIDATING:
            if repairs >= max_repairs:
                raise InvalidTransitionError("repair_budget_exhausted")
            repairs += 1
        if target is S.AWAITING_FINAL_APPROVAL and not self.manifest_sha256:
            raise ChangeValidationError("manifest_missing")
        if (
            target is S.DELIVERING
            and self.valid_approval(ApprovalGate.FINAL, list(approvals)) is None
        ):
            raise ApprovalRequiredError("final_approval_required")
        if target in (S.FAILED, S.BLOCKED) and not failure_code:
            raise ChangeValidationError("failure_code_required")
        return replace(
            self,
            status=target,
            repair_count=repairs,
            failure_code=failure_code if target in (S.FAILED, S.BLOCKED) else self.failure_code,
            row_version=self.row_version + 1,
        )

    def pin_baseline(self, commit_sha: str) -> "CodeChangeAttempt":
        if self.status is not S.PREPARING:
            raise InvalidTransitionError("baseline_pinned_only_while_preparing")
        if not COMMIT_SHA.fullmatch(commit_sha):
            raise ChangeValidationError("commit_sha_invalid")
        if self.base_commit_sha is not None and self.base_commit_sha != commit_sha:
            raise StaleApprovalError("baseline_already_pinned")  # never switches commits
        return replace(self, base_commit_sha=commit_sha, row_version=self.row_version + 1)

    def record_plan(self, ref: str, sha256: str) -> "CodeChangeAttempt":
        if self.status is not S.PLANNING:
            raise InvalidTransitionError("plan_recorded_only_while_planning")
        if not SHA256.fullmatch(sha256):
            raise ChangeValidationError("plan_hash_invalid")
        return replace(self, plan_ref=ref, plan_sha256=sha256, row_version=self.row_version + 1)

    def record_manifest(self, ref: str, sha256: str) -> "CodeChangeAttempt":
        if self.status is not S.VALIDATING:
            raise InvalidTransitionError("manifest_recorded_only_while_validating")
        if not SHA256.fullmatch(sha256):
            raise ChangeValidationError("manifest_hash_invalid")
        return replace(
            self, manifest_ref=ref, manifest_sha256=sha256, row_version=self.row_version + 1
        )

    def decide(
        self,
        gate: ApprovalGate,
        artifact_sha256: str,
        decision: ApprovalDecision,
        actor: str,
        *,
        reason: str | None = None,
    ) -> Approval:
        """An operator decision. Only for the gate the attempt is waiting at and only for the
        artifact it currently holds: a decision about any other hash is refused as stale."""
        waiting = (
            S.AWAITING_PLAN_APPROVAL if gate is ApprovalGate.PLAN else S.AWAITING_FINAL_APPROVAL
        )
        if self.status is not waiting:
            raise InvalidTransitionError("not_awaiting_this_approval")
        if artifact_sha256 != self._artifact(gate):
            raise StaleApprovalError("artifact_changed")
        return Approval(self.id, gate, artifact_sha256, decision, actor, reason=reason)
