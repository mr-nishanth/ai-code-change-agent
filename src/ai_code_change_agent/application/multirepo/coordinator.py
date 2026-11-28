"""Deterministic coordination of a multi-repository Story (no model decides anything here).

* start: resolve EVERY declared repository through the registry (ACTIVE, project mapping,
  published profile) and validate the dependency graph before any child runs. One unresolved
  repository or a bad graph blocks the whole parent: no clone, workspace or model call happens.
* advance: queue the children whose dependencies completed with a published contract; block the
  children whose upstream failed; finish the parent and post one aggregated Jira comment.
* child_finished: record the child's outcome (PR, branch, contract artifact) from its attempt.
Every state change is a compare-and-set on the stored status and every job has a dedupe key, so
a replayed webhook, a crashed worker or two concurrent advances never duplicate work."""

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ai_code_change_agent.agents.coding.schemas import ImplementationPlan
from ai_code_change_agent.application.artifacts.hashing import load_verified, store_model
from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.delivery.manifest import DeliveryManifest
from ai_code_change_agent.application.errors import ArtifactIntegrityError
from ai_code_change_agent.application.jira.comment_lines import (
    clarification_detail,
    safe_line,
    validation_detail,
)
from ai_code_change_agent.application.jobs.queue import JobQueue
from ai_code_change_agent.application.multirepo.models import (
    Child,
    ChildStatus,
    ContractDoc,
    MultiRepoStore,
    Parent,
    ParentStatus,
    ReportState,
)
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.changes.models import ApprovalDecision, CodeChangeAttempt
from ai_code_change_agent.domain.changes.multirepo import (
    DeclarationError,
    RepositoryDeclaration,
    dependency_order,
)
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.domain.repositories.resolution import ResolutionRequest
from ai_code_change_agent.validation.schemas import ValidationReport

_log = get_logger(__name__)
CHILD_JOB = "code_change.child"
REPORT_JOB = "code_change.parent_report"
MAX_REPORT_ATTEMPTS = 6  # per report round; then PERMANENT_FAILURE (operator re-runs it)
REPORT_STALE_SECONDS = 300  # an IN_PROGRESS claim older than this belongs to a crashed worker


class ReportRetryableError(Exception):
    """Raised by the report job so the job queue retries it with backoff."""


def report_marker(parent_id: uuid.UUID, report_round: int) -> str:
    """Stable identity of one aggregated report: written into the comment, searched for
    before any re-post, so an uncertain POST is never blindly repeated."""
    return f"aica-report-{parent_id}-r{report_round}"


_DONE = frozenset({ChildStatus.COMPLETED})
_FAILED = frozenset({ChildStatus.FAILED, ChildStatus.REJECTED, ChildStatus.BLOCKED})
# A stored artifact failed its hash, schema or origin check: possible corruption or tampering
# in the artifact store (artifact_too_large and artifact_storage_error stay retryable).
_INTEGRITY_FAILURES = frozenset(
    {
        "artifact_hash_mismatch",
        "artifact_schema_invalid",
        "artifact_missing",
        "artifact_provenance_mismatch",
        "artifact_integrity_failed",
    }
)
_CHILD_DETAIL_CHARS = 2000  # one aggregated comment holds every child's detail
# All children's detail together: Jira comments are cut at 8000 characters (MAX_COMMENT), and
# the status, reason and retry lines of every child (at most ~350 characters each, up to 20
# children) and the footer must always fit, so the detail gets what they leave.
_COMMENT_CHARS = 7500
_CHILD_LINES_CHARS = 350
_RESULT_TEXT = {
    ParentStatus.COMPLETED: "all repository executions and required contracts completed.",
    ParentStatus.PARTIALLY_COMPLETED: "partially completed; successful PRs are kept.",
}


@dataclass(frozen=True)
class UpstreamContract:
    repository_key: str
    role: str | None
    sha256: str
    text: str


@dataclass(frozen=True)
class ChildContext:
    """What one child's agent is told about the multi-repository change (read-only)."""

    repository_key: str
    role: str | None
    responsibility: str | None
    siblings: tuple[str, ...]
    downstream: tuple[str, ...]
    upstream_contracts: tuple[UpstreamContract, ...]


class MultiRepoBlockedError(Exception):
    def __init__(self, code: str, report: str) -> None:
        super().__init__(code)
        self.code = code
        self.report = report


def _short(ref: str) -> str:
    return ref.rsplit("/", 1)[-1].lower().replace("_", "-").removesuffix(".git")


class Coordinator:
    def __init__(
        self,
        *,
        store: MultiRepoStore,
        changes: CodeChangeService,
        queue: JobQueue,
        artifacts: Any,
        resolver: Any,
        reporter: Any,
        max_repositories: int,
        max_job_attempts: int,
        cost_of_issue: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._store = store
        self._changes = changes
        self._queue = queue
        self._artifacts = artifacts
        self._resolver = resolver
        self._reporter = reporter
        self._max = max_repositories
        self._job_attempts = max_job_attempts
        self._cost = cost_of_issue

    @property
    def store(self) -> MultiRepoStore:
        return self._store

    # ---- start ------------------------------------------------------------------------------
    async def start(
        self,
        request_id: uuid.UUID,
        issue_key: str,
        project_key: str,
        declarations: Sequence[RepositoryDeclaration],
    ) -> Parent:
        existing = await self._store.find_by_request(request_id)
        if existing is not None:  # a replayed intake never creates a second parent
            await self.advance(existing[0].id)
            return existing[0]
        declaration_doc: dict[str, Any] = {
            "repositories": [
                {
                    "reference": d.reference,
                    "role": d.role,
                    "responsibility": d.responsibility,
                    "depends_on": d.depends_on,
                }
                for d in declarations
            ]
        }
        problems: list[str] = []
        if len(declarations) > self._max:
            problems.append(f"too many repositories: {len(declarations)} (limit {self._max})")
        resolved: list[tuple[RepositoryDeclaration, Any]] = []
        for d in declarations[: self._max]:
            result = await self._resolver.resolve(
                ResolutionRequest(issue_key, project_key, (d.reference,))
            )
            if not result.resolved:
                problems.append(f"{d.reference}: {result.outcome.value.lower()} ({result.detail})")
            else:
                resolved.append((d, result))
        keys = [r.repository_key for _, r in resolved]
        if len(set(keys)) != len(keys):
            problems.append("the same repository is declared twice")
        edges: dict[str, list[str]] = {}
        if not problems:
            alias: dict[str, str] = {}
            for d, r in resolved:
                for name in (d.reference.lower(), _short(d.reference), r.repository_key):
                    alias.setdefault(name, r.repository_key)
            for d, r in resolved:
                needs = []
                for dep in d.depends_on:
                    key = alias.get(dep.lower()) or alias.get(_short(dep))
                    if key is None:
                        problems.append(f"{d.reference}: depends on unknown repository '{dep}'")
                    elif key not in needs:  # one edge per upstream, however often it is named
                        needs.append(key)
                edges[r.repository_key] = needs
            if not problems:
                try:
                    order = dependency_order(keys, edges)
                except DeclarationError as exc:
                    problems.append(f"{exc.code} ({exc.detail})")
        if problems:
            declaration_doc["problems"] = problems
            parent = Parent(uuid.uuid4(), request_id, ParentStatus.BLOCKED, declaration_doc)
            parent, _ = await self._store.create(parent, [])
            await self._store.set_request_status(request_id, "BLOCKED")
            text = (
                f"AI Code Change Agent could not start {issue_key} (multi-repository).\n"
                "Nothing was cloned, changed or published. Fix the Story or the registry:\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\nEach repository must be granted to the Git host app, registered, ACTIVE and "
                "mapped to this Jira project by an operator; dependencies must name declared "
                "repositories and form no cycle.\nRetryable: no, the cause must be fixed first."
            )
            raise MultiRepoBlockedError("multi_repository_blocked", text)
        position = {key: i for i, key in enumerate(order)}
        parent = Parent(uuid.uuid4(), request_id, ParentStatus.RUNNING, declaration_doc)
        children = [
            Child(
                id=uuid.uuid4(),
                parent_id=parent.id,
                position=position[r.repository_key],
                repository_id=r.repository_id,
                profile_version_id=r.profile_version_id,
                repository_key=r.repository_key,
                reference=d.reference[:200],
                role=d.role,
                responsibility=d.responsibility,
                depends_on=tuple(edges[r.repository_key]),
                status=ChildStatus.WAITING,
            )
            for d, r in resolved
        ]
        parent, _ = await self._store.create(parent, children)
        await self._store.set_request_status(request_id, "PENDING")
        _log.info(
            "multi_repository_parent_created", parent_id=str(parent.id), repositories=len(children)
        )
        await self.advance(parent.id)
        return parent

    # ---- advance ----------------------------------------------------------------------------
    async def advance(self, parent_id: uuid.UUID) -> None:
        parent, children = await self._store.load(parent_id)
        if parent.status is not ParentStatus.RUNNING:
            return
        by_key = {c.repository_key: c for c in children}
        for child in sorted(children, key=lambda c: c.position):
            if child.status is not ChildStatus.WAITING:
                continue
            upstream = [by_key[k] for k in child.depends_on]
            failed = [u for u in upstream if u.status in _FAILED]
            if failed:
                if await self._store.update_child(
                    child.id,
                    frozenset({ChildStatus.WAITING}),
                    status=ChildStatus.BLOCKED,
                    failure_code=f"upstream_failed:{failed[0].repository_key}"[:120],
                ):  # children run in dependency order: a block cascades within this pass
                    by_key[child.repository_key] = replace(child, status=ChildStatus.BLOCKED)
                continue
            if any(u.status not in _DONE for u in upstream):
                continue
            missing = [u for u in upstream if not u.contract_sha256]
            if missing:  # never let a client guess the API shape
                if await self._store.update_child(
                    child.id,
                    frozenset({ChildStatus.WAITING}),
                    status=ChildStatus.BLOCKED,
                    failure_code=f"upstream_contract_missing:{missing[0].repository_key}"[:120],
                ):
                    by_key[child.repository_key] = replace(child, status=ChildStatus.BLOCKED)
                continue
            if await self._store.update_child(
                child.id, frozenset({ChildStatus.WAITING}), status=ChildStatus.QUEUED
            ):
                await self._queue.enqueue(
                    CHILD_JOB,
                    {"child_id": str(child.id)},
                    dedupe_key=f"{CHILD_JOB}:{child.id}:{child.runs}",
                    max_attempts=self._job_attempts,
                )
        parent, children = await self._store.load(parent_id)
        if children and all(c.terminal for c in children):
            await self._finish(parent, children)

    async def _finish(self, parent: Parent, children: list[Child]) -> None:
        done = [c for c in children if c.status is ChildStatus.COMPLETED]
        status = (
            ParentStatus.COMPLETED
            if len(done) == len(children)
            else ParentStatus.PARTIALLY_COMPLETED
            if done
            else ParentStatus.FAILED
        )
        if await self._store.update_parent(
            parent.id, frozenset({ParentStatus.RUNNING}), status=status, finished=True
        ):
            await self._store.set_request_status(parent.request_id, status.value)
            _log.info(
                "multi_repository_parent_finished", parent_id=str(parent.id), status=status.value
            )
        await self.report(parent.id)

    async def report(self, parent_id: uuid.UUID) -> ReportState:
        """Post the aggregated comment of the current report round to the parent Story, at most
        once. Lifecycle: PENDING -> IN_PROGRESS -> SUCCEEDED | RETRYABLE_FAILURE |
        PERMANENT_FAILURE. A claim left IN_PROGRESS (crash) or RETRYABLE_FAILURE (timeout,
        429, 5xx) may hide a comment Jira already created, so Jira is searched for this round's
        report id first; only a confirmed absence leads to a new POST."""
        parent, children = await self._store.load(parent_id)
        if parent.status is ParentStatus.RUNNING:
            return ReportState(parent.jira_report or ReportState.PENDING)
        claim = await self._store.claim_report(parent.id, REPORT_STALE_SECONDS)
        if claim is None:
            return ReportState(parent.jira_report or ReportState.PENDING)
        request = await self._changes.get_request(parent.request_id)
        issue_key = request.jira_issue_key
        marker = report_marker(parent.id, claim.round)
        log = _log.bind(
            parent_id=str(parent.id), issue_key=issue_key, report_id=marker,
            report_attempt=claim.attempts, previous_state=claim.previous.value,
        )  # fmt: skip
        state, comment_id, error, http_status = ReportState.RETRYABLE_FAILURE, None, None, None
        try:
            found = None
            if claim.previous in (ReportState.IN_PROGRESS, ReportState.RETRYABLE_FAILURE):
                found = await self._reporter.find_comment(issue_key, marker)
                if found is not None:
                    log.info("jira_report_reconciled", lookup=str(found.status))
            result = found
            if result is None:  # confirmed absent (or a first attempt): post this round's report
                text = await self.summary(parent, children, issue_key, marker=marker)
                result = await self._reporter.post_comment(issue_key, text)
            state = {
                "POSTED": ReportState.SUCCEEDED,
                "DISABLED": ReportState.DISABLED,
                "PERMANENT": ReportState.PERMANENT_FAILURE,
            }.get(str(result.status), ReportState.RETRYABLE_FAILURE)
            comment_id, error, http_status = result.comment_id, result.error, result.http_status
            if state is ReportState.RETRYABLE_FAILURE and claim.attempts >= MAX_REPORT_ATTEMPTS:
                state, error = ReportState.PERMANENT_FAILURE, f"retries_exhausted:{error}"
        finally:  # a crash before this line leaves IN_PROGRESS, which the next run reconciles
            await self._store.finish_report(
                parent.id, state, comment_id=comment_id, error=(error or None) and error[:120]
            )
        log.info(
            "jira_report", report_state=state.value, comment_id=comment_id,
            jira_status=http_status, error=error,
        )  # fmt: skip
        if state is ReportState.RETRYABLE_FAILURE:
            await self._queue.enqueue(
                REPORT_JOB,
                {"parent_id": str(parent.id)},
                dedupe_key=f"{REPORT_JOB}:{parent.id}:{claim.round}",
                max_attempts=MAX_REPORT_ATTEMPTS,
            )
        return state

    async def summary(
        self, parent: Parent, children: list[Child], issue_key: str, *, marker: str | None = None
    ) -> str:
        lines = [
            f"AI Code Change Agent Execution: {parent.status.value}",
            f"Issue: {issue_key}",
            "",
            "Repositories:",
        ]
        budget: int | None = max(0, _COMMENT_CHARS - _CHILD_LINES_CHARS * len(children))
        for i, c in enumerate(sorted(children, key=lambda c: c.position), start=1):
            lines.append(f"{i}. {c.repository_key}" + (f" ({c.role})" if c.role else ""))
            lines.append(f"   Status: {c.status.value}")
            if c.status is ChildStatus.COMPLETED:
                lines += [
                    f"   Branch: {c.branch or '-'}",
                    f"   Draft PR: {c.pr_url or 'not recorded'}",
                    "   Validation: PASSED",
                ]
            else:
                retry = "yes (ai-code-change changes retry-child " + str(c.id) + ")"
                if c.failure_code in _INTEGRITY_FAILURES:
                    # Possible corruption or tampering in the artifact store: an operator
                    # investigates before anyone retries the child.
                    retry = (
                        "no (artifact integrity failure: an operator must investigate the "
                        f"artifact store before retry-child {c.id})"
                    )
                if c.failure_code == "story_needs_clarification":
                    retry = (
                        "after the Story is clarified (edit the Story to answer the questions "
                        f"below, then ai-code-change changes retry-child {c.id})"
                    )
                lines += [f"   Reason: {c.failure_code or '-'}", f"   Retryable: {retry}"]
                for ln in await self._child_detail(c):  # whole lines, within the total budget
                    if budget is not None and len(ln) + 4 <= budget:
                        lines.append(f"   {ln}")
                        budget -= len(ln) + 4
                    else:
                        if budget is not None:
                            lines.append("   [... more detail omitted]")
                        budget = None  # every later child's detail is omitted too
                        break
            if c.contract_sha256:
                lines.append(f"   Published contract: sha256 {c.contract_sha256[:12]}")
            if c.runs > 1:
                lines.append(f"   Runs: {c.runs}")
        lines.append("")
        lines.append(
            "Cross-Repository Result: "
            + _RESULT_TEXT.get(parent.status, "no repository was delivered.")
        )
        if self._cost is not None:
            lines += ["", f"Total LLM API Cost: {await self._cost(issue_key)} USD"]
        lines += ["", "No PR was merged automatically. Review and merge are human decisions."]
        if parent.completed_at is not None:
            lines.append(f"Completed at: {parent.completed_at.isoformat(timespec='seconds')}")
        if marker is not None:
            lines.append(f"Report ID: {marker}")
        return "\n".join(lines)

    async def _child_detail(self, c: Child) -> list[str]:
        """Bounded, scanned detail lines of a failed child, from its attempt's latest artifact:
        the planner's summary and questions for a Story that needs clarification, or why
        validation failed, or the reason an operator gave for rejecting it. Best effort: a missing
        or unreadable artifact only omits the detail."""
        code = c.failure_code or ""
        if code == "rejected" and c.attempt_id is not None:
            return await self._operator_reason(c)
        clarify = code == "story_needs_clarification"
        if (
            c.attempt_id is None
            or self._artifacts is None
            or not (clarify or code.startswith("validation"))
        ):
            return []
        kind = ArtifactKind.IMPLEMENTATION_PLAN if clarify else ArtifactKind.VALIDATION_REPORT
        try:
            scope = ArtifactScope(
                (await self._changes.get_attempt(c.attempt_id)).request_id, c.attempt_id
            )
            metas = [m for m in await self._artifacts.list(scope) if m.kind is kind]
            if not metas:
                return []
            latest = max(metas, key=lambda m: (m.created_at, m.id))
            if clarify:
                plan = await load_verified(
                    self._artifacts, scope, latest.id, latest.sha256, ImplementationPlan
                )
                text = clarification_detail(plan)
            else:
                report = await load_verified(
                    self._artifacts, scope, latest.id, latest.sha256, ValidationReport
                )
                text = validation_detail(report)
        except Exception as exc:  # the report is still posted, without this child's detail
            _log.warning("child_detail_unavailable", child_id=str(c.id), code=type(exc).__name__)
            return []
        out: list[str] = []
        for line in text.splitlines():  # whole lines, at most _CHILD_DETAIL_CHARS per child
            if sum(map(len, out)) + len(line) > _CHILD_DETAIL_CHARS:
                break
            out.append(line)
        return out

    async def _operator_reason(self, c: Child) -> list[str]:
        """The reason of the child's latest rejection (`changes reject --reason`), scanned."""
        assert c.attempt_id is not None  # noqa: S101 - checked by the caller
        try:
            approvals = await self._changes.approvals(c.attempt_id)
        except Exception as exc:  # the report is still posted, without this child's detail
            _log.warning("child_detail_unavailable", child_id=str(c.id), code=type(exc).__name__)
            return []
        rejected = [a for a in approvals if a.decision is ApprovalDecision.REJECTED]
        latest = max(rejected, key=lambda a: a.decided_at, default=None)
        if latest is None or not latest.reason:
            return []
        return [f"Reason given by the operator: {safe_line(latest.reason, 300)}"]

    # ---- child lifecycle --------------------------------------------------------------------
    async def child_finished(self, child_id: uuid.UUID, attempt: CodeChangeAttempt) -> None:
        """Record the child's outcome and advance its parent. Idempotent: a duplicate
        notification records nothing new and only re-advances the parent (which finishes and
        reports it at most once), so a worker that died between the two steps is recovered."""
        child = await self._store.child(child_id)
        if child is None:
            return
        if child.terminal:
            await self.advance(child.parent_id)
            return
        fields: dict[str, Any] = {}
        scope = ArtifactScope(attempt.request_id, attempt.id)
        if attempt.status is AttemptStatus.COMPLETED:
            status = ChildStatus.COMPLETED
            fields.update(await self._delivery(scope, attempt))
            contract = await self._publish_contract(child, scope, attempt)
            if contract is not None:
                fields["contract_ref"], fields["contract_sha256"] = contract
        else:
            status = (
                ChildStatus.REJECTED
                if attempt.status is AttemptStatus.REJECTED
                else ChildStatus.FAILED
            )
            fields["failure_code"] = (attempt.failure_code or attempt.status.value.lower())[:120]
        await self._store.update_child(
            child.id, frozenset({ChildStatus.QUEUED, ChildStatus.RUNNING}), status=status, **fields
        )
        await self.advance(child.parent_id)

    async def _delivery(self, scope: ArtifactScope, attempt: CodeChangeAttempt) -> dict[str, Any]:
        out: dict[str, Any] = {}
        metas = [
            m for m in await self._artifacts.list(scope) if m.kind is ArtifactKind.DELIVERY_RESULT
        ]
        if metas:
            latest = max(metas, key=lambda m: (m.created_at, m.id))
            doc = json.loads(await self._artifacts.get(scope, latest.id))
            if isinstance(doc.get("pr_url"), str):
                out["pr_url"] = doc["pr_url"][:300]
        if attempt.manifest_ref and attempt.manifest_sha256:
            manifest = await load_verified(
                self._artifacts,
                scope,
                attempt.manifest_ref,
                attempt.manifest_sha256,
                DeliveryManifest,
            )
            out["branch"] = manifest.branch[:120]
        return out

    async def _publish_contract(
        self, child: Child, scope: ArtifactScope, attempt: CodeChangeAttempt
    ) -> tuple[str, str] | None:
        """The upstream child's contract, taken from its validated attempt's report and stored as
        an immutable artifact. None when it published none."""
        metas = [
            m
            for m in await self._artifacts.list(scope)
            if m.kind is ArtifactKind.IMPLEMENTATION_REPORT
        ]
        if not metas:
            return None
        latest = max(metas, key=lambda m: (m.created_at, m.id))
        report = json.loads(await self._artifacts.get(scope, latest.id))
        text = report.get("published_contract")
        if not isinstance(text, str) or not text.strip():
            return None
        stored = await store_model(
            self._artifacts,
            scope,
            ArtifactKind.CROSS_REPO_CONTRACT,
            ContractDoc(
                parent_id=str(child.parent_id),
                repository_key=child.repository_key,
                role=child.role,
                attempt_id=str(attempt.id),
                implementation_report_sha256=latest.sha256,
                contract=text.strip()[:20_000],
            ),
        )
        return stored.ref, stored.sha256

    async def context_for(self, attempt_id: uuid.UUID) -> ChildContext | None:
        child = await self._store.child_by_attempt(attempt_id)
        if child is None:
            return None
        _, children = await self._store.load(child.parent_id)
        by_key = {c.repository_key: c for c in children}
        contracts = []
        for key in child.depends_on:
            up = by_key[key]
            if not (up.attempt_id and up.contract_ref and up.contract_sha256):
                raise MultiRepoBlockedError("upstream_contract_missing", key)
            request = (await self._changes.get_attempt(up.attempt_id)).request_id
            doc = await load_verified(  # the hash recorded at completion must still match
                self._artifacts,
                ArtifactScope(request, up.attempt_id),
                up.contract_ref,
                up.contract_sha256,
                ContractDoc,
            )
            # The hash proves the bytes are unchanged, not that they came from this upstream:
            # the contract must name this parent, this repository and the recorded attempt.
            if (doc.parent_id, doc.repository_key, doc.attempt_id) != (
                str(child.parent_id),
                key,
                str(up.attempt_id),
            ):
                raise ArtifactIntegrityError("artifact_provenance_mismatch")
            contracts.append(UpstreamContract(key, up.role, up.contract_sha256, doc.contract))
        return ChildContext(
            repository_key=child.repository_key,
            role=child.role,
            responsibility=child.responsibility,
            siblings=tuple(c.repository_key for c in children if c.id != child.id),
            downstream=tuple(
                c.repository_key for c in children if child.repository_key in c.depends_on
            ),
            upstream_contracts=tuple(contracts),
        )

    async def is_child(self, attempt_id: uuid.UUID) -> bool:
        return await self._store.child_by_attempt(attempt_id) is not None

    async def retry(self, child_id: uuid.UUID) -> Child:
        """Re-run one finished, unsuccessful child (and the children it blocked). Completed
        siblings, their branches and PRs are kept; a new attempt gets a new branch."""
        child = await self._store.child(child_id)
        if child is None:
            raise MultiRepoBlockedError("child_not_found", str(child_id))
        if child.status not in _FAILED:
            raise MultiRepoBlockedError("child_not_retryable", child.status.value)
        await self._store.update_child(
            child.id,
            _FAILED,
            status=ChildStatus.WAITING,
            runs=child.runs + 1,
            failure_code=None,
            attempt_id=None,  # a new attempt (new branch); the failed one stays in history
        )
        _, children = await self._store.load(child.parent_id)
        for c in children:
            if c.status is ChildStatus.BLOCKED and (c.failure_code or "").startswith("upstream_"):
                await self._store.update_child(
                    c.id, frozenset({ChildStatus.BLOCKED}), status=ChildStatus.WAITING,
                    runs=c.runs + 1, failure_code=None, attempt_id=None,
                )  # fmt: skip
        parent, _ = await self._store.load(child.parent_id)
        await self._store.update_parent(
            parent.id,
            frozenset(
                {ParentStatus.COMPLETED, ParentStatus.PARTIALLY_COMPLETED, ParentStatus.FAILED}
            ),
            status=ParentStatus.RUNNING,
            reopen=True,
        )
        await self._store.set_request_status(parent.request_id, "PENDING")
        await self.advance(parent.id)
        refreshed = await self._store.child(child_id)
        assert refreshed is not None  # noqa: S101
        return refreshed
