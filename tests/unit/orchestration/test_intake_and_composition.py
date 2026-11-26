"""Intake job handling and the composition root (no external services)."""

import uuid
from pathlib import Path
from typing import Any

import pytest

from ai_code_change_agent.application.jira.errors import JiraIssueNotFoundError
from ai_code_change_agent.application.jobs.queue import Job, JobFailedError
from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.composition import (
    build_baseline_provider,
    build_delivery_publishers,
    build_git_provider_clients,
    build_github_auth,
    build_jira_reader,
    build_jira_reporter,
    build_sandbox_manager,
    build_webhook_service,
    build_workspace_runtime,
    require_database,
)
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.integrations.github.publisher import GitHubAppPublisher
from ai_code_change_agent.integrations.github.references import GitHubReferenceParser
from ai_code_change_agent.orchestration.intake import IntakeHandler, ResumeHandler
from ai_code_change_agent.providers.llm.factory import build_chat_model
from tests.support.changes import MemoryChanges, python_profile
from tests.support.github_repos import reader_rig
from tests.support.registry_fixtures import new_repo
from tests.support.registry_memory import InMemoryRegistry
from tests.support.workflow import FakeJira


async def registry() -> InMemoryRegistry:
    reg = InMemoryRegistry()
    svc = reg.service()
    await svc.register_repository(
        new_repo("shop-service", pid="424242", owner="acme", name="shop-service"), actor="cli:t"
    )
    await svc.create_version("shop-service", python_profile(), actor="cli:t")
    await svc.publish_version("shop-service", 1, actor="cli:t")
    await svc.map_project("SHOP", "shop-service", actor="cli:t")
    await svc.enable("shop-service", actor="cli:t")
    return reg


def job(payload: dict[str, Any]) -> Job:
    return Job(uuid.uuid4(), "code_change.intake", payload, 1, 3)


async def test_intake_resolves_assigns_and_starts_exactly_one_attempt() -> None:
    reg, changes = await registry(), MemoryChanges()
    request = changes.add_request()
    started: list[uuid.UUID] = []

    async def start(attempt_id: uuid.UUID) -> None:
        started.append(attempt_id)

    handler = IntakeHandler(
        changes=changes.service(),
        jira=FakeJira(),
        resolver=RepositoryResolver(reg.uow, parsers=(GitHubReferenceParser(),)),
        start=start,
        repository_field_configured=False,
    )
    payload = {"code_change_request_id": str(request.id)}
    await handler(job(payload))
    await handler(job(payload))  # a redelivered job: same attempt, started again (idempotent)
    assert len(set(started)) == 1
    assert (await changes.service().get_request(request.id)).repository_id is not None


async def test_intake_refuses_bad_payloads_unknown_repos_and_moved_issues() -> None:
    reg, changes = await registry(), MemoryChanges()
    resolver = RepositoryResolver(reg.uow)

    async def start(attempt_id: uuid.UUID) -> None:  # pragma: no cover - never reached
        raise AssertionError

    def handler(jira: Any) -> IntakeHandler:
        return IntakeHandler(
            changes=changes.service(),
            jira=jira,
            resolver=resolver,
            start=start,
            repository_field_configured=False,
        )

    with pytest.raises(JobFailedError, match="payload_invalid"):
        await handler(FakeJira())(job({}))
    other = changes.add_request(project="OPS", key="OPS-1")
    with pytest.raises(JobFailedError, match="jira_project_changed"):
        await handler(FakeJira())(job({"code_change_request_id": str(other.id)}))

    class Missing(FakeJira):
        async def get_issue(self, issue_ref: str) -> Any:
            raise JiraIssueNotFoundError

    req = changes.add_request()
    with pytest.raises(JobFailedError, match="jira_issue_not_found"):
        await handler(Missing())(job({"code_change_request_id": str(req.id)}))
    empty = IntakeHandler(
        changes=changes.service(),
        jira=FakeJira(),
        resolver=RepositoryResolver(InMemoryRegistry().uow),
        start=start,
        repository_field_configured=True,
    )
    with pytest.raises(JobFailedError, match="repository_"):
        await empty(job({"code_change_request_id": str(req.id)}))


async def test_resume_handler_validates_its_payload() -> None:
    seen: list[uuid.UUID] = []

    async def resume(attempt_id: uuid.UUID) -> None:
        seen.append(attempt_id)

    aid = uuid.uuid4()
    await ResumeHandler(resume)(
        Job(uuid.uuid4(), "code_change.resume", {"attempt_id": str(aid)}, 1, 3)
    )
    assert seen == [aid]
    with pytest.raises(JobFailedError):
        await ResumeHandler(resume)(Job(uuid.uuid4(), "code_change.resume", {}, 1, 3))


def settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_composition_without_configuration_fails_fast_or_stays_disabled(tmp_path: Path) -> None:
    s = settings()
    with pytest.raises(ConfigurationError):
        require_database(s)
    with pytest.raises(ConfigurationError):
        build_webhook_service(s, None)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError):
        build_workspace_runtime(s)
    with pytest.raises(ConfigurationError):
        build_chat_model(s)
    assert build_jira_reader(s) is None and build_github_auth(s) is None
    assert build_delivery_publishers(s, None).keys == ()
    assert build_git_provider_clients(None).keys == ()
    assert repr(build_jira_reporter(s)) == "JiraOutcomeReporter(enabled=False)"


def test_composition_wires_one_auth_object_and_least_privilege(tmp_path: Path) -> None:
    rig = reader_rig(tmp_path)
    s = settings(
        github_app_id=1,
        github_app_installation_id=2,
        github_app_private_key_path=str(tmp_path / "app.pem"),
        workspace_root=str(tmp_path / "ws"),
        workspace_image="img:1",
        llm_model="claude-test",
        anthropic_api_key="sk-test",
        jira_base_url="https://x.atlassian.net",
        jira_email="a@b.c",
        jira_api_token="t",
    )
    auth = build_github_auth(s)
    assert auth is not None and rig.auth is not auth
    assert build_delivery_publishers(s, auth).keys == ()  # PR creation off
    on = settings(**(s.model_dump() | {"github_pr_create_enabled": True}))
    assert isinstance(build_delivery_publishers(on, auth).get("github"), GitHubAppPublisher)
    assert build_baseline_provider(lambda: None, auth) is not None  # type: ignore[arg-type,return-value]
    assert build_jira_reader(s) is not None
    assert build_sandbox_manager(s) is not None and build_workspace_runtime(s) is not None
    model = build_chat_model(s)
    assert model.__class__.__name__ == "ChatAnthropic" and "sk-test" not in repr(model)


async def test_an_unresolvable_repository_is_reported_to_jira_once() -> None:
    changes = MemoryChanges()
    req = changes.add_request()

    class Reporter:
        def __init__(self) -> None:
            self.reports: list[tuple[str, str]] = []

        async def report(self, key: str, text: str) -> str:
            self.reports.append((key, text))
            return "POSTED"

    async def start(attempt_id: uuid.UUID) -> None:  # pragma: no cover - never reached
        raise AssertionError

    reporter = Reporter()
    handler = IntakeHandler(
        changes=changes.service(),
        jira=FakeJira(),
        resolver=RepositoryResolver(InMemoryRegistry().uow),
        start=start,
        repository_field_configured=False,
        reporter=reporter,
    )
    with pytest.raises(JobFailedError, match="repository_"):
        await handler(job({"code_change_request_id": str(req.id)}))
    ((key, text),) = reporter.reports
    assert key == req.jira_issue_key and "could not start" in text and "Retryable: no" in text


def test_the_worker_settings_default_to_the_automated_pipeline() -> None:
    s = settings()
    assert s.workflow_approval_mode == "automatic"
    assert s.dependency_index_url == "https://pypi.org/simple"
    assert s.deep_agent_run_timeout_seconds == 5400
    with pytest.raises(ValueError, match="DEPENDENCY_INDEX_URL"):
        settings(dependency_index_url="http://insecure.example/simple")
    with pytest.raises(ValueError, match="DEPENDENCY_INDEX_URL"):
        settings(dependency_index_url="https://user:pw@index.example/simple")
