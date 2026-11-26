"""Multi-repository Stories: declarations, dependency order and the coordinator (fakes only)."""

import uuid
from dataclasses import replace
from typing import Any

import pytest

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
from ai_code_change_agent.domain.changes.multirepo import (
    DeclarationError,
    dependency_order,
    parse_story,
)
from ai_code_change_agent.domain.repositories.resolution import (
    ResolutionOutcome,
    ResolutionResult,
)
from ai_code_change_agent.integrations.jira.reporting import CommentResult, CommentStatus

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
    store: MemStore, coordinator: Coordinator, key: str, status: ChildStatus, contract: bool = True
) -> None:
    child = next(c for c in store.children.values() if c.repository_key == key)
    fields: dict[str, Any] = {"status": status}
    if status is ChildStatus.COMPLETED:
        fields |= {"pr_url": f"https://example.invalid/{key}/pull/1", "branch": f"aica/{key}"}
        if contract:
            fields |= {"contract_ref": "art_x", "contract_sha256": "a" * 64}
    else:
        fields["failure_code"] = "validation_failed"
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
