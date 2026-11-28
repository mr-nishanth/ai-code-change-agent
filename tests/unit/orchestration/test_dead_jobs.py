"""Dead-job hooks: whatever recorded a job DEAD, the request, attempt or child it owned ends, once,
through the existing transitions, and Jira hears of it once (S2119 Part 1)."""

import uuid
from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from ai_code_change_agent.application.jobs.queue import Job, JobFailedError
from ai_code_change_agent.application.multirepo.models import ChildStatus
from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.orchestration.dead_jobs import DEAD_JOB_CODE, AttemptRecovery
from ai_code_change_agent.orchestration.intake import IntakeHandler, intake_report_id
from tests.support.changes import MemoryChanges
from tests.support.jira_reports import CommentJira, MemoryReportStore
from tests.support.registry_memory import InMemoryRegistry
from tests.support.workflow import FakeJira

S = AttemptStatus
EXHAUSTED = "lease_expired_attempts_exhausted"


def dead(kind: str, last_error: str = EXHAUSTED, **payload: Any) -> Job:
    return Job(uuid.uuid4(), kind, {k: str(v) for k, v in payload.items()}, 3, 3, last_error)


async def attempt_in(changes: MemoryChanges, status: AttemptStatus) -> Any:
    req = changes.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    attempt = await changes.service().start_attempt(req.id)
    changes.force(attempt.id, status=status)  # as far as the dead job had taken it
    return req, await changes.service().get_attempt(attempt.id)


@dataclass
class Recorded:
    reported: list[uuid.UUID] = field(default_factory=list)
    completed: list[uuid.UUID] = field(default_factory=list)

    async def report(self, attempt_id: uuid.UUID) -> None:
        self.reported.append(attempt_id)

    async def complete(self, attempt_id: uuid.UUID) -> None:
        self.completed.append(attempt_id)


def recovery(changes: MemoryChanges, rec: Recorded, coordinator: Any = None) -> AttemptRecovery:
    return AttemptRecovery(changes=changes.service(), report=rec.report, complete=rec.complete,
                           coordinator=coordinator)  # fmt: skip


async def test_a_dead_resume_job_fails_its_attempt_once_and_reports_it() -> None:
    changes, rec = MemoryChanges(), Recorded()
    req, attempt = await attempt_in(changes, S.IMPLEMENTING)
    hooks = recovery(changes, rec)
    for _ in range(2):  # the hook may run twice (worker + dispatcher): nothing changes twice
        await hooks.resume_dead(dead("code_change.resume", attempt_id=attempt.id))
    after = await changes.service().get_attempt(attempt.id)
    assert after.status is S.FAILED and after.failure_code == DEAD_JOB_CODE
    request = await changes.service().get_request(req.id)
    assert request.status == "FAILED"  # the request projection is terminal too
    assert rec.reported == [attempt.id, attempt.id]  # the once-per-attempt report dedupes
    # the active-attempt guard is released: a new attempt can start for the request
    assert (await changes.service().start_attempt(req.id)).id != attempt.id
    await hooks.resume_dead(dead("code_change.resume"))  # a bad payload: nothing to end


async def test_an_attempt_in_reporting_is_completed_through_the_report_step() -> None:
    changes, rec = MemoryChanges(), Recorded()
    _, attempt = await attempt_in(changes, S.IMPLEMENTING)
    changes.force(attempt.id, status=S.REPORTING)
    await recovery(changes, rec).end_attempt(attempt.id)
    assert rec.completed == [attempt.id]  # its pull request exists: never marked FAILED


async def test_a_terminal_attempt_is_only_reported() -> None:
    changes, rec = MemoryChanges(), Recorded()
    _, attempt = await attempt_in(changes, S.IMPLEMENTING)
    await changes.service().transition(attempt.id, S.CANCELLED)
    await recovery(changes, rec).end_attempt(attempt.id)
    assert (await changes.service().get_attempt(attempt.id)).status is S.CANCELLED
    assert rec.reported == [attempt.id] and rec.completed == []


@dataclass
class FakeChild:
    id: uuid.UUID
    parent_id: uuid.UUID
    attempt_id: uuid.UUID | None
    status: ChildStatus = ChildStatus.RUNNING

    @property
    def terminal(self) -> bool:
        return self.status in {ChildStatus.COMPLETED, ChildStatus.FAILED, ChildStatus.BLOCKED}


class FakeStore:
    def __init__(self, *children: FakeChild, parent_of: dict[uuid.UUID, Any] | None = None):
        self.children = {c.id: c for c in children}
        self.parent_of = parent_of or {}
        self.updates: list[tuple[uuid.UUID, ChildStatus, str]] = []

    async def child(self, child_id: uuid.UUID) -> FakeChild | None:
        return self.children.get(child_id)

    async def child_by_attempt(self, attempt_id: uuid.UUID) -> FakeChild | None:
        return next((c for c in self.children.values() if c.attempt_id == attempt_id), None)

    async def find_by_request(self, request_id: uuid.UUID) -> Any:
        return self.parent_of.get(request_id)

    async def update_child(self, child_id: uuid.UUID, expected: Any, **fields: Any) -> bool:
        self.updates.append((child_id, fields["status"], fields["failure_code"]))
        self.children[child_id].status = fields["status"]
        return True


class FakeCoordinator:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.finished: list[tuple[uuid.UUID, AttemptStatus]] = []
        self.advanced: list[uuid.UUID] = []

    async def child_finished(self, child_id: uuid.UUID, attempt: Any) -> None:
        self.finished.append((child_id, attempt.status))

    async def advance(self, parent_id: uuid.UUID) -> None:
        self.advanced.append(parent_id)


async def test_a_dead_child_job_fails_its_attempt_and_the_coordinator_records_it() -> None:
    changes, rec = MemoryChanges(), Recorded()
    _, attempt = await attempt_in(changes, S.PLANNING)
    child = FakeChild(uuid.uuid4(), uuid.uuid4(), attempt.id)
    coordinator = FakeCoordinator(FakeStore(child))
    await recovery(changes, rec, coordinator).child_dead(
        dead("code_change.child", child_id=child.id)
    )
    assert coordinator.finished == [(child.id, S.FAILED)]  # the parent advances and reports
    assert rec.reported == []  # a child never reports alone


async def test_a_dead_child_without_an_attempt_fails_and_its_parent_advances() -> None:
    changes, rec = MemoryChanges(), Recorded()
    child = FakeChild(uuid.uuid4(), uuid.uuid4(), None, ChildStatus.QUEUED)
    coordinator = FakeCoordinator(FakeStore(child))
    hooks = recovery(changes, rec, coordinator)
    await hooks.child_dead(dead("code_change.child", child_id=child.id))
    await hooks.child_dead(dead("code_change.child", child_id=child.id))  # terminal: advance only
    assert coordinator.store.updates == [(child.id, ChildStatus.FAILED, DEAD_JOB_CODE)]
    assert coordinator.advanced == [child.parent_id, child.parent_id]
    await hooks.child_dead(dead("code_change.child", child_id=uuid.uuid4()))  # unknown child
    await hooks.child_dead(dead("code_change.child"))  # bad payload


async def test_an_old_child_attempt_of_a_multi_repository_request_is_not_reported_alone() -> None:
    changes, rec = MemoryChanges(), Recorded()
    req, attempt = await attempt_in(changes, S.PLANNING)
    coordinator = FakeCoordinator(FakeStore(parent_of={req.id: ("parent", [])}))
    await recovery(changes, rec, coordinator).end_attempt(attempt.id)
    assert (await changes.service().get_attempt(attempt.id)).status is S.FAILED
    assert rec.reported == []


def durable_intake(changes: MemoryChanges, jira: CommentJira, **kw: Any) -> IntakeHandler:
    from ai_code_change_agent.application.jira.reports import DurableJiraReporter

    async def never(_: uuid.UUID) -> None:
        raise AssertionError("not started")

    return IntakeHandler(
        changes=changes.service(),
        jira=FakeJira(),
        resolver=RepositoryResolver(InMemoryRegistry().uow),
        start=never,
        repository_field_configured=False,
        reporter=DurableJiraReporter(jira.reporter(), MemoryReportStore()),
        **kw,
    )


async def test_a_dead_intake_blocks_its_request_and_reports_once_with_the_code() -> None:
    changes, jira = MemoryChanges(), CommentJira()
    req = changes.add_request()
    handler = durable_intake(changes, jira)
    for _ in range(2):
        await handler.on_dead(dead("code_change.intake", code_change_request_id=req.id))
    assert (await changes.service().get_request(req.id)).status == "BLOCKED"
    (text,) = jira.texts()  # exactly one Jira failure report
    assert "could not start SHOP-1" in text and EXHAUSTED in text
    assert intake_report_id(req.id) in text
    await handler.on_dead(dead("code_change.intake", code_change_request_id=uuid.uuid4()))


async def test_a_refused_intake_already_reported_gets_no_second_comment_from_the_hook() -> None:
    changes, jira = MemoryChanges(), CommentJira()
    req = changes.add_request()
    await changes.service().refuse_request(req.id)  # the refusal blocked (and reported) it
    await durable_intake(changes, jira).on_dead(
        dead("code_change.intake", "repository_unknown_repository", code_change_request_id=req.id)
    )
    assert jira.comments == []


async def test_a_dead_intake_that_started_an_attempt_ends_that_attempt() -> None:
    changes, jira, rec = MemoryChanges(), CommentJira(), Recorded()
    req, attempt = await attempt_in(changes, S.IMPLEMENTING)
    handler = durable_intake(changes, jira, recovery=recovery(changes, rec))
    await handler.on_dead(dead("code_change.intake", code_change_request_id=req.id))
    assert (await changes.service().get_attempt(attempt.id)).status is S.FAILED
    assert rec.reported == [attempt.id] and jira.comments == []  # reported as the attempt


async def test_a_dead_multi_repository_intake_advances_its_parent() -> None:
    changes, jira = MemoryChanges(), CommentJira()
    req = changes.add_request()
    parent = type("P", (), {"id": uuid.uuid4()})()
    coordinator = FakeCoordinator(FakeStore(parent_of={req.id: (parent, [])}))
    handler = durable_intake(changes, jira, coordinator=cast(Any, coordinator))
    await handler.on_dead(dead("code_change.intake", code_change_request_id=req.id))
    assert coordinator.advanced == [parent.id] and jira.comments == []
    assert (await changes.service().get_request(req.id)).status == "RECEIVED"


class FlakyStore(MemoryReportStore):
    """The first report write fails (a DynamoDB outage)."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def create(self, report_id: str, issue_key: str, text: str, sha256: str) -> bool:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("store down")
        return await super().create(report_id, issue_key, text, sha256)


async def test_a_report_store_failure_in_the_dead_hook_never_loses_the_report() -> None:
    """The report is stored before the request is blocked: if storing fails, nothing changed and
    the hook's re-run (dead_settled failed) still blocks and reports."""
    from ai_code_change_agent.application.jira.reports import DurableJiraReporter

    changes, jira = MemoryChanges(), CommentJira()
    req = changes.add_request()
    store = FlakyStore()
    handler = durable_intake(changes, jira)
    handler._reporter = DurableJiraReporter(jira.reporter(), store)
    dead_job = dead("code_change.intake", code_change_request_id=req.id)
    with pytest.raises(RuntimeError):
        await handler.on_dead(dead_job)
    assert (await changes.service().get_request(req.id)).status == "RECEIVED"
    await handler.on_dead(dead_job)  # the hook runs again
    assert (await changes.service().get_request(req.id)).status == "BLOCKED"
    assert len(jira.comments) == 1


async def test_a_report_store_failure_in_a_refusal_retries_the_job_and_reports_once() -> None:
    from ai_code_change_agent.application.jira.reports import DurableJiraReporter
    from tests.support.workflow import UnregisteredRepositoryJira

    changes, jira = MemoryChanges(), CommentJira()
    req = changes.add_request()
    handler = IntakeHandler(
        changes=changes.service(),
        jira=UnregisteredRepositoryJira(),
        resolver=RepositoryResolver(InMemoryRegistry().uow),
        start=lambda _: None,  # type: ignore[arg-type,return-value]
        repository_field_configured=False,
        reporter=DurableJiraReporter(jira.reporter(), FlakyStore()),
    )
    payload = Job(uuid.uuid4(), "code_change.intake", {"code_change_request_id": str(req.id)}, 1, 3)
    with pytest.raises(RuntimeError):
        await handler(payload)
    assert (await changes.service().get_request(req.id)).status == "RECEIVED"
    with pytest.raises(JobFailedError):
        await handler(payload)
    assert (await changes.service().get_request(req.id)).status == "BLOCKED"
    assert len(jira.comments) == 1


async def test_an_attempt_that_died_after_its_pull_request_was_created_is_reported_delivered() -> (
    None
):
    changes, rec = MemoryChanges(), Recorded()
    _, attempt = await attempt_in(changes, S.DELIVERING)

    async def delivered(_: uuid.UUID) -> bool:
        return True

    hooks = AttemptRecovery(changes=changes.service(), report=rec.report, complete=rec.complete,
                            delivered=delivered)  # fmt: skip
    await hooks.end_attempt(attempt.id)
    assert (await changes.service().get_attempt(attempt.id)).status is S.REPORTING
    assert rec.completed == [attempt.id]  # the report step posts the success, COMPLETED


async def test_an_attempt_that_died_in_delivery_without_a_result_says_the_outcome_is_unknown() -> (
    None
):
    changes, rec = MemoryChanges(), Recorded()
    _, attempt = await attempt_in(changes, S.DELIVERING)

    async def delivered(_: uuid.UUID) -> bool:
        return False

    hooks = AttemptRecovery(changes=changes.service(), report=rec.report, complete=rec.complete,
                            delivered=delivered)  # fmt: skip
    await hooks.end_attempt(attempt.id)
    after = await changes.service().get_attempt(attempt.id)
    assert after.status is S.FAILED and after.failure_code == "delivery_outcome_unknown"
    assert rec.reported == [attempt.id] and rec.completed == []


async def test_the_unknown_delivery_comment_says_a_pull_request_may_exist(tmp_path: Any) -> None:
    from ai_code_change_agent.orchestration.steps import CodeChangeSteps
    from tests.support.workflow import make_rig

    rig = await make_rig(tmp_path)
    steps = CodeChangeSteps(rig.deps)
    assert not await steps.pull_request_created(rig.attempt_id)  # no delivery result stored
    rig.changes.force(rig.attempt_id, status=S.FAILED, failure_code="delivery_outcome_unknown")
    await steps.cleanup(rig.attempt_id)
    ((_, text),) = rig.reporter.reports
    assert "may exist" in text and "No pull request was created" not in text
    assert "at the delivery stage" in text


async def test_a_dead_intake_whose_request_got_an_attempt_meanwhile_cancels_its_report() -> None:
    """The report is stored before the request is blocked; when blocking is refused because an
    attempt started meanwhile, the stored "no attempt was started" report is cancelled."""
    from ai_code_change_agent.application.jira.reports import DurableJiraReporter, ReportState

    changes, jira = MemoryChanges(), CommentJira()
    req = changes.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    service = changes.service()
    real = service.refuse_request

    async def raced(request_id: uuid.UUID) -> bool:
        await service.start_attempt(request_id)  # a redelivered intake started one first
        return await real(request_id)

    service.refuse_request = raced  # type: ignore[method-assign]
    store = MemoryReportStore()
    handler = durable_intake(changes, jira)
    handler._changes = service
    handler._reporter = DurableJiraReporter(jira.reporter(), store)
    await handler.on_dead(dead("code_change.intake", code_change_request_id=req.id))
    cancelled = await store.get(intake_report_id(req.id))
    assert cancelled is not None and cancelled.state is ReportState.FAILED_PERMANENT
    assert cancelled.last_error == "report_cancelled"
    assert await handler._reporter.retry_due() == 0 and jira.comments == []


async def test_an_unreadable_delivery_result_counts_as_an_unknown_delivery() -> None:
    changes, rec = MemoryChanges(), Recorded()
    _, attempt = await attempt_in(changes, S.DELIVERING)

    async def broken(_: uuid.UUID) -> bool:
        raise RuntimeError("artifact store down")

    hooks = AttemptRecovery(changes=changes.service(), report=rec.report, complete=rec.complete,
                            delivered=broken)  # fmt: skip
    await hooks.end_attempt(attempt.id)
    after = await changes.service().get_attempt(attempt.id)
    assert after.status is S.FAILED and after.failure_code == "delivery_outcome_unknown"


async def test_a_refusal_whose_request_got_an_attempt_meanwhile_posts_nothing() -> None:
    from ai_code_change_agent.application.jira.reports import DurableJiraReporter
    from tests.support.workflow import UnregisteredRepositoryJira

    changes, jira = MemoryChanges(), CommentJira()
    req = changes.add_request(repository_id=uuid.uuid4(), profile_version_id=uuid.uuid4())
    service = changes.service()
    real = service.refuse_request

    async def raced(request_id: uuid.UUID) -> bool:
        await service.start_attempt(request_id)
        return await real(request_id)

    service.refuse_request = raced  # type: ignore[method-assign]
    handler = IntakeHandler(
        changes=service,
        jira=UnregisteredRepositoryJira(),
        resolver=RepositoryResolver(InMemoryRegistry().uow),
        start=lambda _: None,  # type: ignore[arg-type,return-value]
        repository_field_configured=False,
        reporter=DurableJiraReporter(jira.reporter(), MemoryReportStore()),
    )
    payload = Job(uuid.uuid4(), "code_change.intake", {"code_change_request_id": str(req.id)}, 1, 3)
    with pytest.raises(JobFailedError):
        await handler(payload)
    assert jira.comments == [] and await handler._reporter.retry_due() == 0
