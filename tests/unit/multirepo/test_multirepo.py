"""Multi-repository Stories: declarations, dependency order and the coordinator (fakes only)."""

import uuid
from dataclasses import replace
from typing import Any

import pytest

from ai_code_change_agent.application.multirepo.coordinator import (
    CHILD_JOB,
    Coordinator,
    MultiRepoBlockedError,
)
from ai_code_change_agent.application.multirepo.models import (
    Child,
    ChildStatus,
    Parent,
    ParentStatus,
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

    async def claim_report(self, parent_id: uuid.UUID) -> bool:
        p = self.parents[parent_id]
        if p.jira_report is not None:
            return False
        self.parents[parent_id] = replace(p, jira_report="POSTING")
        return True

    async def finish_report(self, parent_id: uuid.UUID, posted: bool) -> None:
        self.reports += int(posted)
        self.parents[parent_id] = replace(
            self.parents[parent_id], jira_report="POSTED" if posted else None
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
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    async def report(self, issue_key: str, text: str) -> str:
        self.texts.append((issue_key, text))
        return "POSTED"


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
