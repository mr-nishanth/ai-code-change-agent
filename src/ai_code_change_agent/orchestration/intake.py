"""Job handlers: intake of an accepted Jira Story, and resuming a paused workflow.

Intake never trusts the webhook payload: it re-reads the Story from Jira, resolves the
repository deterministically through the registry (no automatic registration), assigns it,
creates (or re-uses) the single active attempt and starts the parent workflow."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.jira.errors import JiraIssueNotFoundError
from ai_code_change_agent.application.jira.ports import JiraIssueReader
from ai_code_change_agent.application.jobs.queue import Job, JobFailedError
from ai_code_change_agent.application.multirepo.coordinator import (
    CHILD_JOB,
    Coordinator,
    MultiRepoBlockedError,
)
from ai_code_change_agent.application.multirepo.models import ChildStatus
from ai_code_change_agent.application.repositories.resolver import (
    RepositoryResolver,
    build_resolution_request,
)
from ai_code_change_agent.domain.changes.multirepo import DeclarationError, parse_story

INTAKE = "code_change.intake"
RESUME = "code_change.resume"
CHILD = CHILD_JOB
REPOSITORY_FIELD = "repository"  # logical name of the configured Jira custom field
Launch = Callable[[uuid.UUID], Awaitable[Any]]


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
            raise JobFailedError("jira_issue_not_found") from None
        if issue.project is None or issue.project.key != request.jira_project_key:
            raise JobFailedError("jira_project_changed")
        story_text = f"{issue.summary or ''}\n{issue.description.text if issue.description else ''}"
        try:
            declarations = parse_story(story_text)
        except DeclarationError as exc:
            await self._report_blocked(issue.key, f"invalid repository declarations: {exc}")
            raise JobFailedError(exc.code) from None
        if len(declarations) > 1:
            if self._coordinator is None:
                raise JobFailedError("multi_repository_disabled")
            try:
                await self._coordinator.start(
                    request_id, issue.key, request.jira_project_key, declarations
                )
            except MultiRepoBlockedError as exc:
                if self._reporter is not None:
                    await self._reporter.report(issue.key, exc.report)
                raise JobFailedError(exc.code) from None
            return
        resolution = build_resolution_request(issue, REPOSITORY_FIELD if self._field else None)
        result = await self._resolver.resolve(resolution)
        if not result.resolved:
            code = f"repository_{result.outcome.value.lower()}"
            if self._reporter is not None:
                await self._reporter.report(
                    issue.key,
                    f"AI Code Change Agent could not start {issue.key}.\n"
                    f"Reason: {code} ({result.detail}).\n"
                    "Name the target repository in the description, for example "
                    "'Repository: owner/name'. It must be granted to the GitHub App, registered, "
                    "ACTIVE and mapped to this Jira project by an operator.\n"
                    "Retryable: no, the cause must be fixed first.",
                )
            raise JobFailedError(code)
        assert result.repository_id and result.profile_version_id  # noqa: S101 - resolved
        await self._changes.assign_repository(
            request_id, result.repository_id, result.profile_version_id
        )
        attempt = await self._changes.start_attempt(request_id)
        await self._start(attempt.id)

    async def _report_blocked(self, issue_key: str, reason: str) -> None:
        if self._reporter is not None:
            await self._reporter.report(
                issue_key,
                f"AI Code Change Agent could not start {issue_key}.\nReason: {reason}.\n"
                "Retryable: no, the cause must be fixed first.",
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


class ResumeHandler:
    def __init__(self, resume: Launch) -> None:
        self._resume = resume

    async def __call__(self, job: Job) -> None:
        try:
            attempt_id = uuid.UUID(str(job.payload["attempt_id"]))
        except (KeyError, ValueError):
            raise JobFailedError("payload_invalid") from None
        await self._resume(attempt_id)
