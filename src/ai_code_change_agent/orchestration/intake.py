"""Job handlers: intake of an accepted Jira Story, and resuming a paused workflow.

Intake never trusts the webhook payload: it re-reads the Story from Jira, resolves the
repository deterministically through the registry (no automatic registration), assigns it,
creates (or re-uses) the single active attempt and starts the parent workflow."""

import contextlib
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.jira.errors import (
    JiraIntegrationError,
    JiraIssueNotFoundError,
    JiraResponseError,
)
from ai_code_change_agent.application.jira.ports import JiraIssueReader
from ai_code_change_agent.application.jobs.queue import Job, JobFailedError, JobRetryableError
from ai_code_change_agent.application.multirepo.coordinator import (
    CHILD_JOB,
    REPORT_JOB,
    Coordinator,
    MultiRepoBlockedError,
    ReportRetryableError,
)
from ai_code_change_agent.application.multirepo.models import ChildStatus, ReportState
from ai_code_change_agent.application.repositories.resolver import (
    RepositoryResolver,
    build_resolution_request,
)
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.domain.changes.errors import InvalidTransitionError
from ai_code_change_agent.domain.changes.models import CodeChangeRequest
from ai_code_change_agent.domain.changes.multirepo import DeclarationError, parse_story
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.orchestration.langgraph.runner import CheckpointExpiredError

INTAKE = "code_change.intake"
RESUME = "code_change.resume"
CHILD = CHILD_JOB
PARENT_REPORT = REPORT_JOB
REPOSITORY_FIELD = "repository"  # logical name of the configured Jira custom field
Launch = Callable[[uuid.UUID], Awaitable[Any]]
# Responses the client treats as unusable that are usually transient (a proxy or maintenance page,
# a truncated body, 408 Request Timeout, 425 Too Early): retried within the job's attempts.
_TRANSIENT_RESPONSES = frozenset({"unexpected_content_type", "malformed_json"})
_TRANSIENT_STATUSES = frozenset({408, 425})
_log = get_logger("intake")


def _transient(exc: JiraIntegrationError) -> bool:
    return isinstance(exc, JiraResponseError) and (
        str(exc) in _TRANSIENT_RESPONSES or exc.status_code in _TRANSIENT_STATUSES
    )


class IntakeHandler:
    def __init__(
        self,
        *,
        changes: CodeChangeService,
        jira: JiraIssueReader,
        resolver: RepositoryResolver,
        start: Launch,
        repository_field_configured: bool,
        reporter: Any = None,  # JiraOutcomeReporter: one comment when no attempt can start
        coordinator: Coordinator | None = None,  # multi-repository Stories
    ) -> None:
        self._coordinator = coordinator
        self._reporter = reporter
        self._changes = changes
        self._jira = jira
        self._resolver = resolver
        self._start = start
        self._field = repository_field_configured

    async def __call__(self, job: Job) -> None:
        try:
            request_id = uuid.UUID(str(job.payload["code_change_request_id"]))
        except (KeyError, ValueError):
            raise JobFailedError("payload_invalid") from None
        request = await self._changes.get_request(request_id)
        try:
            issue = await self._jira.get_issue(request.jira_issue_key)
        except JiraIssueNotFoundError:
            raise await self._refused(request, "jira_issue_not_found") from None
        except JiraIntegrationError as exc:
            # Authentication, permission, a bad request or an unusable response will not fix
            # itself on a retry: dead at once, with the fixed code. Transient errors (429, 5xx
            # outages, timeouts) propagate unchanged, so the queue backs off and retries them.
            if _transient(exc):
                raise JobRetryableError(exc.code) from None
            if not exc.retryable:
                raise await self._refused(request, exc.code) from None
            raise
        if issue.project is None or issue.project.key != request.jira_project_key:
            raise await self._refused(request, "jira_project_changed")
        story_text = f"{issue.summary or ''}\n{issue.description.text if issue.description else ''}"
        try:
            declarations = parse_story(story_text)
        except DeclarationError as exc:
            text = _not_started(issue.key, f"invalid repository declarations: {exc}")
            raise await self._refused(request, exc.code, issue.key, text) from None
        if len(declarations) > 1:
            if self._coordinator is None:
                raise await self._refused(request, "multi_repository_disabled")
            try:
                await self._coordinator.start(
                    request_id, issue.key, request.jira_project_key, declarations
                )
            except MultiRepoBlockedError as exc:
                raise await self._refused(request, exc.code, issue.key, exc.report) from None
            return
        resolution = build_resolution_request(issue, REPOSITORY_FIELD if self._field else None)
        result = await self._resolver.resolve(resolution)
        if not result.resolved:
            code = f"repository_{result.outcome.value.lower()}"
            text = (
                f"AI Code Change Agent could not start {issue.key}.\n"
                f"Reason: {code} ({result.detail}).\n"
                "Name the target repository in the description, for example "
                "'Repository: owner/name'. It must be granted to the GitHub App, registered, "
                "ACTIVE and mapped to this Jira project by an operator.\n"
                "Retryable: no, the cause must be fixed first."
            )
            raise await self._refused(request, code, issue.key, text)
        assert result.repository_id and result.profile_version_id  # noqa: S101 - resolved
        await self._changes.assign_repository(
            request_id, result.repository_id, result.profile_version_id
        )
        attempt = await self._changes.start_attempt(request_id)
        await self._start(attempt.id)

    async def on_dead(self, job: Job) -> None:
        """The worker's hook for an intake job it recorded DEAD for any reason (a refusal, or a
        transient Jira error or an unexpected exception on the last attempt): no attempt will
        start from this intake, so the request ends BLOCKED instead of RECEIVED (idempotent; a
        request that already has an attempt keeps its status). No Jira comment: the cause is
        only a code in the job."""
        try:
            request_id = uuid.UUID(str(job.payload["code_change_request_id"]))
        except (KeyError, ValueError):
            return
        await self._changes.refuse_request(request_id)

    async def _refused(
        self,
        request: CodeChangeRequest,
        code: str,
        issue_key: str | None = None,
        text: str | None = None,
    ) -> JobFailedError:
        """The intake job ends DEAD with `code`, so no attempt will start from it: the request
        becomes BLOCKED (idempotent) before Jira is told, so a storage failure here retries the
        job without posting a comment. A request already BLOCKED when this run began was refused
        and reported by an earlier run (its lease expired before the job failed): no second
        comment. Jira hears of a refusal at most once."""
        changed = await self._changes.refuse_request(request.id)
        first = changed or request.status != AttemptStatus.BLOCKED.value
        if first and text is not None and issue_key is not None and self._reporter is not None:
            await self._reporter.report(issue_key, text)
        return JobFailedError(code)


def _not_started(issue_key: str, reason: str) -> str:
    return (
        f"AI Code Change Agent could not start {issue_key}.\nReason: {reason}.\n"
        "Retryable: no, the cause must be fixed first."
    )


class ChildHandler:
    """Runs one multi-repository child: its own attempt, workspace, agent context, validation
    and draft PR. Re-running the job (crash, lease expiry) resumes the same attempt."""

    def __init__(
        self, *, coordinator: Coordinator, changes: CodeChangeService, start: Launch
    ) -> None:
        self._coordinator = coordinator
        self._changes = changes
        self._start = start

    async def __call__(self, job: Job) -> None:
        try:
            child_id = uuid.UUID(str(job.payload["child_id"]))
        except (KeyError, ValueError):
            raise JobFailedError("payload_invalid") from None
        store = self._coordinator.store
        child = await store.child(child_id)
        if child is None:
            raise JobFailedError("child_not_found")
        if child.terminal:
            await self._coordinator.advance(child.parent_id)
            return
        if child.attempt_id is not None:
            previous = await self._changes.get_attempt(child.attempt_id)
            if previous.terminal:  # finished before the worker could record it
                await self._coordinator.child_finished(child.id, previous)
                return
        parent, _ = await store.load(child.parent_id)
        attempt = await self._changes.start_repository_attempt(
            parent.request_id, child.repository_id, child.profile_version_id
        )
        await store.update_child(
            child.id,
            frozenset({ChildStatus.QUEUED, ChildStatus.RUNNING}),
            status=ChildStatus.RUNNING,
            attempt_id=attempt.id,
        )
        await self._start(attempt.id)
        finished = await self._changes.get_attempt(attempt.id)
        if finished.terminal:
            await self._coordinator.child_finished(child.id, finished)


class ParentReportHandler:
    """Retries the aggregated Jira comment of a multi-repository parent (never re-runs a child).
    A retryable outcome raises, so the job queue backs off and runs it again; a worker crash
    leaves the job leased, and the next claim reconciles with Jira before any new POST."""

    def __init__(self, *, coordinator: Coordinator) -> None:
        self._coordinator = coordinator

    async def __call__(self, job: Job) -> None:
        try:
            parent_id = uuid.UUID(str(job.payload["parent_id"]))
        except (KeyError, ValueError):
            raise JobFailedError("payload_invalid") from None
        state = await self._coordinator.report(parent_id)
        if state is ReportState.RETRYABLE_FAILURE:
            raise ReportRetryableError("jira_report_retryable")


class ResumeHandler:
    """Resumes an attempt after an approval. When the workflow's checkpoint has expired
    (ACCA_CHECKPOINT_TTL_DAYS) the attempt cannot resume: it becomes FAILED with
    `checkpoint_expired`, which releases its active guard, is reported to Jira through the
    workflow's own once-per-attempt report (`report`, the cleanup step), and the job ends with
    that code.

    A multi-repository child that paused at a gate is finished here: once the resumed (or
    expired) attempt is terminal the coordinator records the child and advances its parent, so
    the parent never stays RUNNING after its last child. Children report only through the
    parent's aggregated comment, including an old attempt that retry-child unlinked from its
    child (its request still has the parent). `around` wraps the run (the cost report), so it
    already sees the expired attempt as FAILED."""

    def __init__(
        self,
        resume: Launch,
        *,
        changes: CodeChangeService | None = None,
        coordinator: Coordinator | None = None,
        report: Launch | None = None,
        around: Callable[[Launch], Launch] | None = None,
    ) -> None:
        self._resume = resume
        self._changes = changes
        self._coordinator = coordinator
        self._report = report
        self._run = (around or (lambda launch: launch))(self._resume_or_expire)

    async def __call__(self, job: Job) -> None:
        try:
            attempt_id = uuid.UUID(str(job.payload["attempt_id"]))
        except (KeyError, ValueError):
            raise JobFailedError("payload_invalid") from None
        try:
            await self._run(attempt_id)
        except CheckpointExpiredError as exc:
            _log.warning("attempt_checkpoint_expired", attempt_id=str(attempt_id))
            if (
                not await self._child_finished(attempt_id)
                and self._report is not None
                and not await self._of_multi_repo_request(attempt_id)
            ):
                await self._report(attempt_id)
            raise JobFailedError(exc.code) from None
        await self._child_finished(attempt_id)

    async def _resume_or_expire(self, attempt_id: uuid.UUID) -> Any:
        try:
            return await self._resume(attempt_id)
        except CheckpointExpiredError as exc:
            if self._changes is not None:
                with contextlib.suppress(InvalidTransitionError):  # already terminal
                    await self._changes.transition(
                        attempt_id, AttemptStatus.FAILED, failure_code=exc.code
                    )
            raise

    async def _of_multi_repo_request(self, attempt_id: uuid.UUID) -> bool:
        """True when the attempt's request has a multi-repository parent. Covers an attempt no
        child links any more (retry-child cleared the link): it was a child's, so it is reported
        only through the parent's aggregated comment, never as a single-repository failure."""
        if self._coordinator is None or self._changes is None:
            return False
        attempt = await self._changes.get_attempt(attempt_id)
        return await self._coordinator.store.find_by_request(attempt.request_id) is not None

    async def _child_finished(self, attempt_id: uuid.UUID) -> bool:
        """True when the attempt belongs to a multi-repository child (notified if terminal)."""
        if self._coordinator is None or self._changes is None:
            return False
        child = await self._coordinator.store.child_by_attempt(attempt_id)
        if child is None:
            return False
        attempt = await self._changes.get_attempt(attempt_id)
        if attempt.terminal:  # still at a gate: nothing to record yet (manual approval)
            await self._coordinator.child_finished(child.id, attempt)
        return True
