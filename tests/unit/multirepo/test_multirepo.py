"""Multi-repository Stories: declarations, dependency order and the coordinator (fakes only)."""

import json
import os
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from ai_code_change_agent.application.artifacts.models import ArtifactKind, ArtifactScope
from ai_code_change_agent.application.artifacts.service import ArtifactService
from ai_code_change_agent.application.errors import ArtifactIntegrityError
from ai_code_change_agent.application.multirepo.coordinator import (
    CHILD_JOB,
    REPORT_JOB,
    Coordinator,
    MultiRepoBlockedError,
    report_marker,
)
from ai_code_change_agent.application.multirepo.models import (
    Child,
    ChildStatus,
    Parent,
    ParentStatus,
    ReportClaim,
    ReportState,
)
from ai_code_change_agent.domain.changes.models import CodeChangeAttempt
from ai_code_change_agent.domain.changes.multirepo import (
    DeclarationError,
    dependency_order,
    parse_story,
)
from ai_code_change_agent.domain.changes.states import AttemptStatus
from ai_code_change_agent.domain.repositories.resolution import (
    ResolutionOutcome,
    ResolutionResult,
)
from ai_code_change_agent.execution.artifacts import LocalFilesystemArtifactStore
from ai_code_change_agent.integrations.jira.reporting import CommentResult, CommentStatus
from ai_code_change_agent.orchestration.intake import ChildHandler
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.runner import WorkflowRunner
from ai_code_change_agent.orchestration.steps import CodeChangeSteps
from tests.support.workflow import make_rig

STORY = """Implement order tracking across the existing applications.

Repositories:

- Repository: acme/backend-api
  Role: backend
  Responsibility: create tracking endpoints and response schemas
  Depends on: none

- Repository: acme/web-app
  Role: frontend
  Responsibility: implement the order tracking interface
  Depends on: backend-api

- Repository: acme/mobile-app
  Role: mobile

Dependencies:

- mobile-app depends on backend-api

Requirements:
- Keep API contracts consistent.
"""


def test_declarations_roles_and_dependencies_are_parsed() -> None:
    ds = parse_story(STORY)
    assert [d.reference for d in ds] == ["acme/backend-api", "acme/web-app", "acme/mobile-app"]
    assert [d.role for d in ds] == ["backend", "frontend", "mobile"]
    assert ds[0].depends_on == [] and ds[1].depends_on == ["backend-api"]
    assert ds[2].depends_on == ["backend-api"]  # from the Dependencies section
    assert ds[0].responsibility == "create tracking endpoints and response schemas"


def test_a_single_marker_keeps_the_single_repository_path() -> None:
    assert len(parse_story("Do X.\nRepository: acme/shop_service\n")) == 1
    assert parse_story("no markers here, mentions repo names in prose only") == []


def test_a_dependency_on_an_undeclared_repository_is_refused() -> None:
    with pytest.raises(DeclarationError):
        parse_story("Repository: a/x\nRepository: a/y\nDependencies:\n- z depends on x\n")


def test_dependency_order_detects_cycles_and_unknown_nodes() -> None:
    assert dependency_order(["web", "api", "mobile"], {"web": ["api"], "mobile": ["api"]}) == [
        "api", "mobile", "web",
    ]  # fmt: skip
    with pytest.raises(DeclarationError, match="dependency_cycle"):
        dependency_order(["a", "b"], {"a": ["b"], "b": ["a"]})
    with pytest.raises(DeclarationError, match="dependency_cycle"):
        dependency_order(["a"], {"a": ["a"]})
    with pytest.raises(DeclarationError, match="unknown"):
        dependency_order(["a"], {"a": ["b"]})


# ---- coordinator with in-memory fakes ------------------------------------------------------


class MemStore:
    def __init__(self) -> None:
        self.parents: dict[uuid.UUID, Parent] = {}
        self.children: dict[uuid.UUID, Child] = {}
        self.request_status: dict[uuid.UUID, str] = {}
        self.reports = 0
        self.stale = False  # simulate an IN_PROGRESS claim whose worker crashed

    async def create(self, parent: Parent, children: list[Child]) -> tuple[Parent, list[Child]]:
        found = await self.find_by_request(parent.request_id)
        if found:
            return found
        self.parents[parent.id] = parent
        for c in children:
            self.children[c.id] = c
        return parent, children

    async def find_by_request(self, request_id: uuid.UUID) -> tuple[Parent, list[Child]] | None:
        for p in self.parents.values():
            if p.request_id == request_id:
                return p, self._of(p.id)
        return None

    def _of(self, pid: uuid.UUID) -> list[Child]:
        return sorted(
            (c for c in self.children.values() if c.parent_id == pid), key=lambda c: c.position
        )

    async def load(self, parent_id: uuid.UUID) -> tuple[Parent, list[Child]]:
        return self.parents[parent_id], self._of(parent_id)

    async def child(self, child_id: uuid.UUID) -> Child | None:
        return self.children.get(child_id)

    async def child_by_attempt(self, attempt_id: uuid.UUID) -> Child | None:
        return next((c for c in self.children.values() if c.attempt_id == attempt_id), None)

    async def update_child(
        self, child_id: uuid.UUID, expected: frozenset[ChildStatus], **f: Any
    ) -> bool:
        c = self.children[child_id]
        if c.status not in expected:
            return False
        self.children[child_id] = replace(c, **f)
        return True

    async def update_parent(
        self, parent_id: uuid.UUID, expected: frozenset[ParentStatus], **f: Any
    ) -> bool:
        p = self.parents[parent_id]
        if p.status not in expected:
            return False
        jira = None if f.get("reopen") else p.jira_report
        self.parents[parent_id] = replace(p, status=f.get("status", p.status), jira_report=jira)
        return True

    async def claim_report(self, parent_id: uuid.UUID, stale_seconds: int) -> ReportClaim | None:
        p = self.parents[parent_id]
        previous = ReportState(p.jira_report or "PENDING")
        stale = previous is ReportState.IN_PROGRESS and self.stale
        if previous not in (ReportState.PENDING, ReportState.RETRYABLE_FAILURE) and not stale:
            return None
        attempts = p.jira_report_attempts + 1
        self.parents[parent_id] = replace(
            p, jira_report="IN_PROGRESS", jira_report_attempts=attempts
        )
        return ReportClaim(p.report_round, previous, attempts)

    async def finish_report(
        self,
        parent_id: uuid.UUID,
        state: ReportState,
        *,
        comment_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.reports += int(state is ReportState.SUCCEEDED)
        p = self.parents[parent_id]
        self.parents[parent_id] = replace(
            p,
            jira_report=state.value,
            jira_comment_id=comment_id or p.jira_comment_id,
            jira_report_error=error,
        )

    async def set_request_status(self, request_id: uuid.UUID, status: str) -> None:
        self.request_status[request_id] = status


class Queue:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    async def enqueue(
        self, kind: str, payload: dict[str, Any], *, dedupe_key: str, max_attempts: int
    ) -> bool:
        if dedupe_key in self.jobs:
            return False
        self.jobs[dedupe_key] = payload
        return True


class Resolver:
    def __init__(self, denied: set[str] | None = None) -> None:
        self.denied = denied or set()

    async def resolve(self, req: Any) -> ResolutionResult:
        ref = req.references[0]
        key = ref.rsplit("/", 1)[-1]
        if ref in self.denied:
            return ResolutionResult(ResolutionOutcome.PROJECT_NOT_AUTHORISED, "no_active_mapping")
        rid = uuid.uuid5(uuid.NAMESPACE_URL, key)
        return ResolutionResult(
            ResolutionOutcome.RESOLVED, "ok", rid, key, uuid.uuid4(), 1, "0" * 64
        )


class Reporter:
    """Fake Jira: records POSTs; outcomes scripted per call; comments searchable by text."""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.outcomes: list[CommentResult] = []  # next POST results (default: 201)
        self.lookup: CommentResult | None = None  # forced lookup result (e.g. 503)
        self.lookups = 0
        self.lost_response = False  # Jira creates the comment but the response is lost

    async def report(self, issue_key: str, text: str) -> str:
        self.texts.append((issue_key, text))
        return "POSTED"

    async def post_comment(self, issue_key: str, text: str) -> CommentResult:
        if self.outcomes:
            result = self.outcomes.pop(0)
            if result.status is not CommentStatus.POSTED and not self.lost_response:
                return result
            self.texts.append((issue_key, text))
            return result
        self.texts.append((issue_key, text))
        return CommentResult(CommentStatus.POSTED, comment_id=str(10000 + len(self.texts)))

    async def find_comment(self, issue_key: str, marker: str) -> CommentResult | None:
        self.lookups += 1
        if self.lookup is not None:
            return self.lookup
        for i, (key, text) in enumerate(self.texts):
            if key == issue_key and marker in text:
                return CommentResult(CommentStatus.POSTED, comment_id=str(10001 + i))
        return None


class Changes:
    async def get_request(self, request_id: uuid.UUID) -> Any:
        class R:
            jira_issue_key = "SHOP-100"

        return R()


def make(denied: set[str] | None = None) -> tuple[Coordinator, MemStore, Queue, Reporter]:
    store, queue, reporter = MemStore(), Queue(), Reporter()
    coordinator = Coordinator(
        store=store, changes=Changes(), queue=queue, artifacts=None,  # type: ignore[arg-type]
        resolver=Resolver(denied), reporter=reporter, max_repositories=5, max_job_attempts=3,
    )  # fmt: skip
    return coordinator, store, queue, reporter


async def finish(
    store: MemStore,
    coordinator: Coordinator,
    key: str,
    status: ChildStatus,
    contract: bool = True,
    failure_code: str = "validation_failed",
) -> None:
    child = next(c for c in store.children.values() if c.repository_key == key)
    fields: dict[str, Any] = {"status": status}
    if status is ChildStatus.COMPLETED:
        fields |= {"pr_url": f"https://example.invalid/{key}/pull/1", "branch": f"aica/{key}"}
        if contract:
            fields |= {"contract_ref": "art_x", "contract_sha256": "a" * 64}
    else:
        fields["failure_code"] = failure_code
    await store.update_child(
        child.id, frozenset({ChildStatus.QUEUED, ChildStatus.RUNNING}), **fields
    )
    await coordinator.advance(child.parent_id)


async def test_upstream_runs_first_then_dependents_fan_out_and_one_report_is_posted() -> None:
    coordinator, store, queue, reporter = make()
    request = uuid.uuid4()
    parent = await coordinator.start(request, "SHOP-100", "SHOP", parse_story(STORY))
    queued = {store.children[uuid.UUID(p["child_id"])].repository_key for p in queue.jobs.values()}
    assert queued == {"backend-api"}  # web and mobile wait for the backend contract
    await finish(store, coordinator, "backend-api", ChildStatus.COMPLETED)
    queued = {store.children[uuid.UUID(p["child_id"])].repository_key for p in queue.jobs.values()}
    assert queued == {"backend-api", "web-app", "mobile-app"}  # both clients in parallel
    assert all(k.startswith(CHILD_JOB) for k in queue.jobs)
    await finish(store, coordinator, "web-app", ChildStatus.COMPLETED)
    assert reporter.texts == []  # not finished yet
    await finish(store, coordinator, "mobile-app", ChildStatus.COMPLETED)
    assert store.parents[parent.id].status is ParentStatus.COMPLETED
    assert len(reporter.texts) == 1 and "Execution: COMPLETED" in reporter.texts[0][1]
    assert reporter.texts[0][1].count("Draft PR: https://") == 3
    await coordinator.advance(parent.id)  # replay: nothing new, no second comment
    assert len(reporter.texts) == 1 and len(queue.jobs) == 3


async def test_replayed_intake_reuses_the_parent() -> None:
    coordinator, store, queue, _ = make()
    request = uuid.uuid4()
    first = await coordinator.start(request, "SHOP-100", "SHOP", parse_story(STORY))
    again = await coordinator.start(request, "SHOP-100", "SHOP", parse_story(STORY))
    assert first.id == again.id and len(store.parents) == 1 and len(queue.jobs) == 1


async def test_an_unauthorised_repository_blocks_everything_before_any_work() -> None:
    coordinator, store, queue, _ = make(denied={"acme/web-app"})
    with pytest.raises(MultiRepoBlockedError) as err:
        await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(STORY))
    assert "acme/web-app" in err.value.report and queue.jobs == {}
    (parent,) = store.parents.values()
    assert parent.status is ParentStatus.BLOCKED and store.children == {}


async def test_a_failed_upstream_blocks_dependents_and_a_missing_contract_never_lets_them_guess() -> (
    None
):
    coordinator, store, _, reporter = make()
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(STORY))
    await finish(store, coordinator, "backend-api", ChildStatus.COMPLETED, contract=False)
    codes = {c.repository_key: c.failure_code for c in store.children.values()}
    assert codes["web-app"] == "upstream_contract_missing:backend-api"
    assert store.parents[parent.id].status is ParentStatus.PARTIALLY_COMPLETED
    assert "Retryable: yes" in reporter.texts[0][1]


async def test_partial_failure_keeps_siblings_and_retry_reruns_only_the_failed_child() -> None:
    coordinator, store, queue, reporter = make()
    story = "Repository: acme/backend-api\nRepository: acme/web-app\n"
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(story))
    assert len(queue.jobs) == 2  # independent: both start at once
    await finish(store, coordinator, "backend-api", ChildStatus.COMPLETED)
    await finish(store, coordinator, "web-app", ChildStatus.FAILED)
    assert store.parents[parent.id].status is ParentStatus.PARTIALLY_COMPLETED
    web = next(c for c in store.children.values() if c.repository_key == "web-app")
    await store.update_child(web.id, frozenset({ChildStatus.FAILED}), attempt_id=uuid.uuid4())
    retried = await coordinator.retry(web.id)
    assert retried.status is ChildStatus.QUEUED and retried.runs == 1
    assert retried.attempt_id is None  # the next job starts a new attempt, never the failed one
    assert len(queue.jobs) == 3  # one new job, for the failed child only
    backend = next(c for c in store.children.values() if c.repository_key == "backend-api")
    assert backend.status is ChildStatus.COMPLETED and backend.pr_url  # sibling untouched
    await finish(store, coordinator, "web-app", ChildStatus.COMPLETED)
    assert store.parents[parent.id].status is ParentStatus.COMPLETED
    assert len(reporter.texts) == 2  # one comment per report round
    with pytest.raises(MultiRepoBlockedError, match="child_not_retryable"):
        await coordinator.retry(web.id)


async def test_too_many_repositories_and_cycles_are_refused() -> None:
    coordinator, _, queue, _ = make()
    many = "".join(f"Repository: acme/r{i}\n" for i in range(6))
    with pytest.raises(MultiRepoBlockedError):
        await coordinator.start(uuid.uuid4(), "SHOP-1", "SHOP", parse_story(many))
    cyc = "Repository: acme/a\n  Depends on: b\nRepository: acme/b\n  Depends on: a\n"
    with pytest.raises(MultiRepoBlockedError) as err:
        await coordinator.start(uuid.uuid4(), "SHOP-2", "SHOP", parse_story(cyc))
    assert "dependency_cycle" in err.value.report and queue.jobs == {}


# ---- Jira reporting lifecycle -----------------------------------------------------------------


async def finished_parent(
    outcomes: list[CommentResult] | None = None, *, lost_response: bool = False
) -> tuple[Coordinator, MemStore, Queue, Reporter, Parent]:
    coordinator, store, queue, reporter = make()
    reporter.outcomes = list(outcomes or [])
    reporter.lost_response = lost_response
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(STORY))
    for key in ("backend-api", "web-app", "mobile-app"):
        await finish(store, coordinator, key, ChildStatus.COMPLETED)
    return coordinator, store, queue, reporter, store.parents[parent.id]


R = CommentStatus


async def test_success_posts_once_to_the_parent_story_and_persists_the_comment_id() -> None:
    _, _store, _, reporter, parent = await finished_parent()
    assert [k for k, _ in reporter.texts] == ["SHOP-100"]
    text = reporter.texts[0][1]
    assert report_marker(parent.id, parent.report_round) in text
    assert parent.jira_report == "SUCCEEDED" and parent.jira_comment_id == "10001"
    assert reporter.lookups == 0  # a first attempt never needs reconciliation


@pytest.mark.parametrize("code", [401, 403, 404])
async def test_auth_and_permission_errors_are_permanent_and_not_retried(code: int) -> None:
    result = CommentResult(R.PERMANENT, http_status=code, error=f"http_{code}")
    coordinator, _, queue, reporter, parent = await finished_parent([result])
    assert parent.jira_report == "PERMANENT_FAILURE" and parent.jira_report_error == f"http_{code}"
    assert parent.status is ParentStatus.COMPLETED  # execution status is untouched
    assert not any(k.startswith(REPORT_JOB) for k in queue.jobs)
    assert await coordinator.report(parent.id) is ReportState.PERMANENT_FAILURE
    assert reporter.texts == []


@pytest.mark.parametrize(
    "result",
    [
        CommentResult(R.RETRYABLE, http_status=429, error="http_429"),
        CommentResult(R.RETRYABLE, http_status=503, error="http_503"),
        CommentResult(R.RETRYABLE, error="timeout"),
    ],
)
async def test_transient_failures_schedule_a_report_job_and_retry_reconciles_first(
    result: CommentResult,
) -> None:
    coordinator, store, queue, reporter, parent = await finished_parent([result])
    assert parent.jira_report == "RETRYABLE_FAILURE"
    jobs = [k for k in queue.jobs if k.startswith(REPORT_JOB)]
    assert jobs == [f"{REPORT_JOB}:{parent.id}:{parent.report_round}"]
    assert await coordinator.report(parent.id) is ReportState.SUCCEEDED
    assert reporter.lookups == 1 and len(reporter.texts) == 1
    assert store.parents[parent.id].status is ParentStatus.COMPLETED


async def test_timeout_after_jira_created_the_comment_never_posts_a_duplicate() -> None:
    lost = CommentResult(R.RETRYABLE, error="timeout")
    coordinator, store, _, reporter, parent = await finished_parent([lost], lost_response=True)
    assert len(reporter.texts) == 1 and parent.jira_report == "RETRYABLE_FAILURE"
    assert await coordinator.report(parent.id) is ReportState.SUCCEEDED
    assert len(reporter.texts) == 1  # found by its report id, not posted again
    assert store.parents[parent.id].jira_comment_id == "10001"


async def test_worker_crash_after_jira_accepted_is_reconciled_from_jira() -> None:
    coordinator, store, _, reporter, parent = await finished_parent()
    # simulate: POST landed, then the worker died before finish_report was committed
    store.parents[parent.id] = replace(parent, jira_report="IN_PROGRESS", jira_comment_id=None)
    assert await coordinator.report(parent.id) is ReportState.IN_PROGRESS  # live claim: hands off
    store.stale = True  # the claim outlived REPORT_STALE_SECONDS: its worker is gone
    assert await coordinator.report(parent.id) is ReportState.SUCCEEDED
    assert len(reporter.texts) == 1 and store.parents[parent.id].jira_comment_id == "10001"


async def test_worker_crash_before_the_post_posts_exactly_once_on_recovery() -> None:
    coordinator, store, _, reporter, parent = await finished_parent(
        [CommentResult(R.RETRYABLE, error="transport_error")]
    )
    store.parents[parent.id] = replace(store.parents[parent.id], jira_report="IN_PROGRESS")
    store.stale = True
    assert await coordinator.report(parent.id) is ReportState.SUCCEEDED
    assert reporter.lookups == 1 and len(reporter.texts) == 1


async def test_failed_jira_lookup_never_posts_blindly() -> None:
    coordinator, store, _, reporter, parent = await finished_parent(
        [CommentResult(R.RETRYABLE, error="timeout")]
    )
    reporter.lookup = CommentResult(R.RETRYABLE, http_status=503, error="lookup_http_503")
    assert await coordinator.report(parent.id) is ReportState.RETRYABLE_FAILURE
    assert reporter.texts == []
    assert store.parents[parent.id].jira_report_error == "lookup_http_503"


async def test_retries_are_bounded_then_permanent() -> None:
    flaky = [CommentResult(R.RETRYABLE, http_status=503, error="http_503")] * 10
    coordinator, store, _, _reporter, parent = await finished_parent(flaky)
    states = [await coordinator.report(parent.id) for _ in range(8)]
    assert states[-1] is ReportState.PERMANENT_FAILURE
    final = store.parents[parent.id]
    assert final.jira_report_attempts == 6
    assert (final.jira_report_error or "").startswith("retries_")


async def test_duplicate_report_calls_produce_one_comment() -> None:
    coordinator, _, _, reporter, parent = await finished_parent()
    for _ in range(3):
        await coordinator.report(parent.id)
        await coordinator.advance(parent.id)
    assert len(reporter.texts) == 1


async def test_failed_child_with_successful_sibling_reports_partial_not_completed() -> None:
    coordinator, store, _, reporter = make()
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(STORY))
    await finish(store, coordinator, "backend-api", ChildStatus.COMPLETED)
    await finish(store, coordinator, "web-app", ChildStatus.COMPLETED)
    await finish(store, coordinator, "mobile-app", ChildStatus.FAILED)
    text = reporter.texts[-1][1]
    assert "Execution: PARTIALLY_COMPLETED" in text and "Execution: COMPLETED" not in text
    assert "Reason: validation_failed" in text and "retry-child" in text
    assert store.parents[parent.id].status is ParentStatus.PARTIALLY_COMPLETED


@pytest.mark.parametrize(
    "code", ["dependency_conflict", "dependency_hash_mismatch", "dependency_metadata_unavailable"]
)
async def test_a_dependency_failure_in_one_repository_keeps_the_sibling_and_retries_alone(
    code: str,
) -> None:
    coordinator, store, queue, reporter = make()
    story = "Repository: acme/backend-api\nRepository: acme/web-app\n"
    await coordinator.start(uuid.uuid4(), "SHOP-101", "SHOP", parse_story(story))
    await finish(store, coordinator, "backend-api", ChildStatus.COMPLETED)
    await finish(store, coordinator, "web-app", ChildStatus.FAILED, failure_code=code)
    text = reporter.texts[-1][1]
    assert "Execution: PARTIALLY_COMPLETED" in text and f"Reason: {code}" in text
    web = next(c for c in store.children.values() if c.repository_key == "web-app")
    jobs_before = len(queue.jobs)
    await coordinator.retry(web.id)
    assert len(queue.jobs) == jobs_before + 1  # only the failed repository is re-queued
    backend = next(c for c in store.children.values() if c.repository_key == "backend-api")
    assert backend.status is ChildStatus.COMPLETED and backend.runs == 0


async def test_missing_pr_url_is_reported_as_not_recorded() -> None:
    coordinator, store, _, reporter = make()
    await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(STORY))
    for key in ("backend-api", "web-app", "mobile-app"):
        await finish(store, coordinator, key, ChildStatus.COMPLETED)
    assert reporter.texts  # sanity
    child = next(c for c in store.children.values() if c.repository_key == "web-app")
    parent, children = await store.load(child.parent_id)
    children = [replace(c, pr_url=None) if c.id == child.id else c for c in children]
    text = await coordinator.summary(parent, children, "SHOP-100")
    assert "Draft PR: not recorded" in text


async def test_report_job_raises_for_retryable_so_the_queue_backs_off() -> None:
    from ai_code_change_agent.application.jobs.queue import JobFailedError
    from ai_code_change_agent.orchestration.intake import ParentReportHandler

    coordinator, store, _, _, parent = await finished_parent(
        [CommentResult(R.RETRYABLE, http_status=429, error="http_429")] * 2
    )
    handler = ParentReportHandler(coordinator=coordinator)
    job: Any = type("J", (), {"payload": {"parent_id": str(parent.id)}})()
    with pytest.raises(Exception, match="jira_report_retryable"):
        await handler(job)
    await handler(job)  # third POST succeeds after reconciliation found nothing
    assert store.parents[parent.id].jira_report == "SUCCEEDED"
    with pytest.raises(JobFailedError):
        await handler(type("J", (), {"payload": {}})())


# ---- cross-repository contracts, chains, failures and resumption ----------------------------

CHAIN = """Repository: acme/web-app
  Depends on: backend-api
Repository: acme/backend-api
  Depends on: schema-lib
Repository: acme/schema-lib
"""  # declared downstream-first on purpose: the order comes from the graph, not the text
FAN = """Repository: acme/backend-api
Repository: acme/web-app
  Depends on: backend-api
Repository: acme/worker
"""  # web-app needs backend-api; worker is independent of both
CONTRACT = "GET /orders/{id} -> {id: str, status: str}"


class AttemptChanges(Changes):
    """Changes fake that also knows the attempts the children ran (for context_for)."""

    def __init__(self) -> None:
        self.attempts: dict[uuid.UUID, CodeChangeAttempt] = {}

    async def get_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt:
        return self.attempts[attempt_id]


def artifact_service(root: Path) -> ArtifactService:
    return ArtifactService(
        LocalFilesystemArtifactStore(root, max_artifact_bytes=5_000_000),
        max_artifact_bytes=5_000_000,
        max_attempt_bytes=50_000_000,
    )


def make_real(
    artifacts: ArtifactService,
) -> tuple[Coordinator, MemStore, Queue, Reporter, AttemptChanges]:
    store, queue, reporter, changes = MemStore(), Queue(), Reporter(), AttemptChanges()
    coordinator = Coordinator(
        store=store, changes=changes, queue=queue, artifacts=artifacts,  # type: ignore[arg-type]
        resolver=Resolver(), reporter=reporter, max_repositories=5, max_job_attempts=3,
    )  # fmt: skip
    return coordinator, store, queue, reporter, changes


def by_key(store: MemStore, key: str) -> Child:
    return next(c for c in store.children.values() if c.repository_key == key)


def queued_keys(store: MemStore, queue: Queue) -> list[str]:
    return [store.children[uuid.UUID(p["child_id"])].repository_key for p in queue.jobs.values()]


async def begin(
    store: MemStore, changes: AttemptChanges, key: str, attempt_id: uuid.UUID | None = None
) -> CodeChangeAttempt:
    """What the child job does before the workflow runs: an attempt, recorded on the child."""
    child = by_key(store, key)
    parent = store.parents[child.parent_id]
    attempt = CodeChangeAttempt(
        attempt_id or uuid.uuid4(), parent.request_id, 1, child.repository_id,
        child.profile_version_id, status=AttemptStatus.IMPLEMENTING,
    )  # fmt: skip
    changes.attempts[attempt.id] = attempt
    assert await store.update_child(
        child.id, frozenset({ChildStatus.QUEUED}), status=ChildStatus.RUNNING, attempt_id=attempt.id
    )
    return attempt


async def complete_with_report(
    coordinator: Coordinator,
    store: MemStore,
    changes: AttemptChanges,
    artifacts: ArtifactService,
    key: str,
    report: dict[str, Any],
) -> CodeChangeAttempt:
    """The real publication path: the attempt's implementation report -> child_finished."""
    attempt = await begin(store, changes, key)
    scope = ArtifactScope(attempt.request_id, attempt.id)
    await artifacts.put(scope, ArtifactKind.IMPLEMENTATION_REPORT, json.dumps(report).encode())
    attempt = replace(attempt, status=AttemptStatus.COMPLETED)
    changes.attempts[attempt.id] = attempt
    await coordinator.child_finished(by_key(store, key).id, attempt)
    return attempt


async def published_backend(
    tmp_path: Path, story: str = FAN
) -> tuple[Coordinator, MemStore, Queue, Reporter, AttemptChanges, ArtifactService]:
    artifacts = artifact_service(tmp_path / "artifacts")
    coordinator, store, queue, reporter, changes = make_real(artifacts)
    await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(story))
    await complete_with_report(
        coordinator, store, changes, artifacts, "backend-api", {"published_contract": CONTRACT}
    )
    return coordinator, store, queue, reporter, changes, artifacts


async def test_a_published_contract_reaches_only_the_children_that_depend_on_it(
    tmp_path: Path,
) -> None:
    coordinator, store, _, _, changes, _ = await published_backend(tmp_path)
    backend = by_key(store, "backend-api")
    assert backend.status is ChildStatus.COMPLETED and backend.contract_sha256
    web = await begin(store, changes, "web-app")
    ctx = await coordinator.context_for(web.id)
    assert ctx is not None
    assert [(c.repository_key, c.text) for c in ctx.upstream_contracts] == [
        ("backend-api", CONTRACT)
    ]
    worker = await begin(store, changes, "worker")
    ctx = await coordinator.context_for(worker.id)
    assert ctx is not None and ctx.upstream_contracts == ()  # it does not depend on backend-api
    assert ctx.downstream == () and "backend-api" in ctx.siblings


@pytest.mark.parametrize("tamper", ["recorded_hash", "stored_bytes"])
async def test_a_contract_whose_content_no_longer_matches_its_hash_is_refused(
    tmp_path: Path, tamper: str
) -> None:
    coordinator, store, _, _, changes, _ = await published_backend(tmp_path)
    backend = by_key(store, "backend-api")
    assert backend.contract_ref and backend.contract_sha256
    if tamper == "recorded_hash":
        store.children[backend.id] = replace(backend, contract_sha256="0" * 64)
    else:  # the immutable bytes were edited after the hash was recorded
        (blob,) = (tmp_path / "artifacts").rglob(f"{backend.contract_ref}.blob")
        os.chmod(blob, 0o600)
        blob.write_bytes(blob.read_bytes().replace(b"status", b"STATUS"))
    web = await begin(store, changes, "web-app")
    with pytest.raises(ArtifactIntegrityError) as err:
        await coordinator.context_for(web.id)
    assert err.value.code == "artifact_hash_mismatch"


async def test_a_tampered_contract_fails_the_dependent_before_any_agent_or_workspace(
    tmp_path: Path,
) -> None:
    """Real workflow steps: the dependent's planning step refuses the contract, so no sandbox
    starts and no agent runs; the child fails and the parent reports it."""
    rig = await make_rig(tmp_path)
    coordinator, store, _, reporter, changes = make_real(rig.artifacts)
    await coordinator.start(
        uuid.uuid4(), "SHOP-100", "SHOP",
        parse_story("Repository: acme/backend-api\nRepository: acme/web-app\n"
                    "  Depends on: backend-api\n"),
    )  # fmt: skip
    await complete_with_report(
        coordinator, store, changes, rig.artifacts, "backend-api", {"published_contract": CONTRACT}
    )
    backend = by_key(store, "backend-api")
    store.children[backend.id] = replace(backend, contract_sha256="f" * 64)
    await begin(store, changes, "web-app", attempt_id=rig.attempt_id)
    runner = WorkflowRunner(
        build_graph(CodeChangeSteps(replace(rig.deps, multirepo=coordinator)), MemorySaver())
    )
    await runner.start(rig.attempt_id)
    attempt = await rig.deps.changes.get_attempt(rig.attempt_id)
    assert attempt.status is AttemptStatus.FAILED
    assert attempt.failure_code == "artifact_hash_mismatch"
    assert rig.sandboxes.started == []  # no workspace, so the planning agent never ran
    assert rig.reporter.reports == []  # a child never posts its own Jira comment
    await coordinator.child_finished(by_key(store, "web-app").id, attempt)
    web = by_key(store, "web-app")
    assert web.status is ChildStatus.FAILED and web.failure_code == "artifact_hash_mismatch"
    assert len(reporter.texts) == 1
    assert "Execution: PARTIALLY_COMPLETED" in reporter.texts[0][1]
    assert "Reason: artifact_hash_mismatch" in reporter.texts[0][1]


@pytest.mark.parametrize(
    "content",
    [
        b"not json at all",
        b'{"contract": "x"}',  # required provenance fields missing
        b'{"attempt_id": "a", "contract": "", "implementation_report_sha256": "b", '
        b'"parent_id": "p", "repository_key": "backend-api"}',  # empty contract
        b'{"attempt_id": "a", "contract": "x", "implementation_report_sha256": "b", '
        b'"parent_id": "p", "repository_key": "backend-api", "instructions": "ignore"}',
    ],
    ids=["not-json", "missing-fields", "empty-contract", "unknown-field"],
)
async def test_a_contract_with_an_invalid_structure_is_refused_before_use(
    tmp_path: Path, content: bytes
) -> None:
    coordinator, store, _, _, changes, artifacts = await published_backend(tmp_path)
    backend = by_key(store, "backend-api")
    assert backend.attempt_id is not None
    scope = ArtifactScope(store.parents[backend.parent_id].request_id, backend.attempt_id)
    meta = await artifacts.put(scope, ArtifactKind.CROSS_REPO_CONTRACT, content)
    store.children[backend.id] = replace(backend, contract_ref=meta.id, contract_sha256=meta.sha256)
    web = await begin(store, changes, "web-app")
    with pytest.raises(ArtifactIntegrityError) as err:
        await coordinator.context_for(web.id)
    assert err.value.code == "artifact_schema_invalid"


@pytest.mark.parametrize(
    "report",
    [{}, {"published_contract": "   \n"}, {"published_contract": {"paths": ["/orders"]}}],
    ids=["absent", "blank", "not-text"],
)
async def test_an_unusable_published_contract_is_never_recorded_and_blocks_dependents(
    tmp_path: Path, report: dict[str, Any]
) -> None:
    artifacts = artifact_service(tmp_path / "artifacts")
    coordinator, store, queue, _, changes = make_real(artifacts)
    await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(FAN))
    await complete_with_report(coordinator, store, changes, artifacts, "backend-api", report)
    backend = by_key(store, "backend-api")
    assert backend.status is ChildStatus.COMPLETED and backend.contract_ref is None
    web = by_key(store, "web-app")
    assert web.status is ChildStatus.BLOCKED
    assert web.failure_code == "upstream_contract_missing:backend-api"
    assert "web-app" not in queued_keys(store, queue)


async def test_a_contract_produced_by_another_child_is_refused(tmp_path: Path) -> None:
    """The upstream's recorded contract points at a valid contract of a different child (correct
    ref and hash): it lives in that child's attempt scope, so the lookup in the declared
    upstream's scope finds nothing and the dependent gets no contract."""
    coordinator, store, _, _, changes, artifacts = await published_backend(tmp_path)
    await complete_with_report(
        coordinator, store, changes, artifacts, "worker", {"published_contract": "QUEUE jobs"}
    )
    worker = by_key(store, "worker")
    assert worker.contract_ref and worker.contract_sha256
    backend = by_key(store, "backend-api")
    store.children[backend.id] = replace(
        backend, contract_ref=worker.contract_ref, contract_sha256=worker.contract_sha256
    )
    web = await begin(store, changes, "web-app")
    with pytest.raises(ArtifactIntegrityError) as err:
        await coordinator.context_for(web.id)
    assert err.value.code == "artifact_missing"


async def test_a_dependent_whose_upstream_has_no_recorded_contract_cannot_start(
    tmp_path: Path,
) -> None:
    coordinator, store, _, _, changes, _ = await published_backend(tmp_path)
    web = await begin(store, changes, "web-app")
    backend = by_key(store, "backend-api")
    store.children[backend.id] = replace(backend, contract_ref=None, contract_sha256=None)
    with pytest.raises(MultiRepoBlockedError, match="upstream_contract_missing"):
        await coordinator.context_for(web.id)


async def test_a_three_level_chain_runs_strictly_in_dependency_order() -> None:
    coordinator, store, queue, reporter = make()
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(CHAIN))
    positions = {c.repository_key: c.position for c in store.children.values()}
    assert positions == {"schema-lib": 0, "backend-api": 1, "web-app": 2}
    assert queued_keys(store, queue) == ["schema-lib"]
    await coordinator.advance(parent.id)  # a replayed advance starts nothing early
    assert queued_keys(store, queue) == ["schema-lib"]
    await finish(store, coordinator, "schema-lib", ChildStatus.COMPLETED)
    assert queued_keys(store, queue) == ["schema-lib", "backend-api"]
    assert by_key(store, "web-app").status is ChildStatus.WAITING  # B has no contract yet
    await finish(store, coordinator, "backend-api", ChildStatus.COMPLETED)
    assert queued_keys(store, queue) == ["schema-lib", "backend-api", "web-app"]
    await finish(store, coordinator, "web-app", ChildStatus.COMPLETED)
    assert store.parents[parent.id].status is ParentStatus.COMPLETED
    (text,) = [t for _, t in reporter.texts]
    assert text.index("1. schema-lib") < text.index("2. backend-api") < text.index("3. web-app")


async def test_a_missing_contract_at_the_top_of_a_chain_blocks_every_level_below() -> None:
    coordinator, store, queue, reporter = make()
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(CHAIN))
    await finish(store, coordinator, "schema-lib", ChildStatus.COMPLETED, contract=False)
    assert by_key(store, "backend-api").failure_code == "upstream_contract_missing:schema-lib"
    assert by_key(store, "web-app").failure_code == "upstream_failed:backend-api"
    assert queued_keys(store, queue) == ["schema-lib"]  # neither level below ever ran
    assert store.parents[parent.id].status is ParentStatus.PARTIALLY_COMPLETED
    assert len(reporter.texts) == 1


@pytest.mark.parametrize("outcome", [ChildStatus.FAILED, ChildStatus.REJECTED])
async def test_an_upstream_that_fails_blocks_dependents_and_the_independent_sibling_still_runs(
    outcome: ChildStatus,
) -> None:
    coordinator, store, queue, reporter = make()
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(FAN))
    assert queued_keys(store, queue) == ["backend-api", "worker"]
    await finish(store, coordinator, "backend-api", outcome, failure_code="validation_failed")
    web = by_key(store, "web-app")
    assert web.status is ChildStatus.BLOCKED and web.failure_code == "upstream_failed:backend-api"
    assert web.attempt_id is None and web.runs == 0  # never started
    assert queued_keys(store, queue) == ["backend-api", "worker"]
    assert reporter.texts == []  # the independent worker is still running
    await finish(store, coordinator, "worker", ChildStatus.COMPLETED)
    assert by_key(store, "worker").status is ChildStatus.COMPLETED
    assert store.parents[parent.id].status is ParentStatus.PARTIALLY_COMPLETED
    assert "web-app" not in queued_keys(store, queue)


async def test_a_duplicate_dependency_edge_is_deduplicated(tmp_path: Path) -> None:
    story = (
        "Repository: acme/backend-api\nRepository: acme/web-app\n"
        "  Depends on: backend-api, acme/backend-api, backend-api\n"
        "Dependencies:\n- web-app depends on backend-api\n"
    )
    coordinator, store, _, _, changes, _ = await published_backend(tmp_path, story)
    assert by_key(store, "web-app").depends_on == ("backend-api",)
    web = await begin(store, changes, "web-app")
    ctx = await coordinator.context_for(web.id)
    assert ctx is not None and [c.repository_key for c in ctx.upstream_contracts] == [
        "backend-api"
    ]  # the agent is given the contract once


async def test_several_failures_and_a_block_are_each_reported_in_one_comment() -> None:
    coordinator, store, queue, reporter = make()
    parent = await coordinator.start(uuid.uuid4(), "SHOP-100", "SHOP", parse_story(FAN))
    await finish(store, coordinator, "backend-api", ChildStatus.FAILED)
    await finish(
        store, coordinator, "worker", ChildStatus.FAILED, failure_code="dependency_conflict"
    )
    final = store.parents[parent.id]
    assert final.status is ParentStatus.FAILED
    assert store.request_status[final.request_id] == "FAILED"
    (text,) = [t for _, t in reporter.texts]
    assert "Execution: FAILED" in text and "COMPLETED" not in text
    for key, status, reason in [
        ("backend-api", "FAILED", "validation_failed"),
        ("web-app", "BLOCKED", "upstream_failed:backend-api"),
        ("worker", "FAILED", "dependency_conflict"),
    ]:
        section = text.split(f". {key}", 1)[1].split("\n\n", 1)[0]
        block = section.split("\n", 4)
        assert f"Status: {status}" in block[1] and f"Reason: {reason}" in block[2]
    assert text.count("Retryable: yes") == 3
    assert "Cross-Repository Result: no repository was delivered." in text
    await coordinator.advance(parent.id)
    assert len(reporter.texts) == 1 and len(queue.jobs) == 2


class HandlerChanges:
    """What ChildHandler needs: attempts, and a new attempt per started child."""

    def __init__(self, store: MemStore) -> None:
        self.store = store
        self.attempts: dict[uuid.UUID, CodeChangeAttempt] = {}
        self.started: list[uuid.UUID] = []

    async def get_attempt(self, attempt_id: uuid.UUID) -> CodeChangeAttempt:
        return self.attempts[attempt_id]

    async def start_repository_attempt(
        self, request_id: uuid.UUID, repository_id: uuid.UUID, profile_version_id: uuid.UUID
    ) -> CodeChangeAttempt:
        attempt = CodeChangeAttempt(uuid.uuid4(), request_id, 1, repository_id, profile_version_id)
        self.attempts[attempt.id] = attempt
        return attempt

    async def launch(self, attempt_id: uuid.UUID) -> None:
        self.started.append(attempt_id)
        self.attempts[attempt_id] = replace(
            self.attempts[attempt_id], status=AttemptStatus.COMPLETED
        )


async def test_a_resumed_parent_never_reruns_completed_children(tmp_path: Path) -> None:
    artifacts = artifact_service(tmp_path / "artifacts")
    coordinator, store, queue, reporter, _ = make_real(artifacts)
    changes = HandlerChanges(store)
    handler = ChildHandler(
        coordinator=coordinator,
        changes=changes,  # type: ignore[arg-type]
        start=changes.launch,
    )
    request = uuid.uuid4()
    parent = await coordinator.start(request, "SHOP-100", "SHOP", parse_story(FAN))
    await finish(store, coordinator, "backend-api", ChildStatus.COMPLETED)
    # interruption: the worker's attempt finished, but the process died before recording it
    worker = by_key(store, "worker")
    done = CodeChangeAttempt(
        uuid.uuid4(), request, 1, worker.repository_id, worker.profile_version_id,
        status=AttemptStatus.COMPLETED,
    )  # fmt: skip
    changes.attempts[done.id] = done
    await store.update_child(
        worker.id, frozenset({ChildStatus.QUEUED}), status=ChildStatus.RUNNING, attempt_id=done.id
    )
    jobs = dict(queue.jobs)
    assert set(queued_keys(store, queue)) == {"backend-api", "web-app", "worker"}
    again = await coordinator.start(request, "SHOP-100", "SHOP", parse_story(FAN))  # replay
    assert again.id == parent.id and queue.jobs == jobs
    for payload in list(jobs.values()):  # every leased job runs again after the restart
        await handler(type("J", (), {"payload": payload})())
    backend, web, worker = (by_key(store, k) for k in ("backend-api", "web-app", "worker"))
    assert backend.runs == 0 and backend.attempt_id is None  # completed: never re-executed
    assert backend.pr_url == "https://example.invalid/backend-api/pull/1"
    assert worker.status is ChildStatus.COMPLETED and worker.attempt_id == done.id
    assert changes.started == [web.attempt_id]  # only the pending child ran
    assert web.status is ChildStatus.COMPLETED and web.runs == 0
    assert queue.jobs == jobs  # no new child job was needed
    assert store.parents[parent.id].status is ParentStatus.COMPLETED
    assert len(reporter.texts) == 1
    for payload in list(jobs.values()):  # a second replay changes nothing
        await handler(type("J", (), {"payload": payload})())
    assert changes.started == [web.attempt_id] and len(reporter.texts) == 1
