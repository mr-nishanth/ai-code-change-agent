"""Intake job handling and the composition root (no external services)."""

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_code_change_agent.application.jira.errors import (
    JiraIssueNotFoundError,
    JiraRateLimitError,
    JiraServiceUnavailableError,
    JiraTimeoutError,
)
from ai_code_change_agent.application.jobs.queue import Job, JobFailedError, JobRetryableError
from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.composition import (
    build_baseline_provider,
    build_delivery_publishers,
    build_git_provider_clients,
    build_github_auth,
    build_jira_reader,
    build_jira_reporter,
    build_reference_tools,
    build_sandbox_manager,
    build_webhook_service,
    build_workspace_runtime,
    require_database,
)
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.integrations.github.publisher import GitHubAppPublisher
from ai_code_change_agent.integrations.github.references import GitHubReferenceParser
from ai_code_change_agent.integrations.jira.issues import JiraCloudIssueReader
from ai_code_change_agent.integrations.web.fetcher import HttpReferenceFetcher
from ai_code_change_agent.orchestration.intake import IntakeHandler, ResumeHandler
from ai_code_change_agent.providers.llm.factory import build_chat_model
from tests.support.changes import MemoryChanges, python_profile
from tests.support.github_repos import reader_rig
from tests.support.jira_rest import make_client
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


def _jira_answering(
    response: Callable[[], httpx.Response],
) -> tuple[JiraCloudIssueReader, list[int]]:
    """The real Jira reader over a scripted transport; returns it and its request counter."""
    calls = [0]

    def handler(_: httpx.Request) -> httpx.Response:
        calls[0] += 1
        return response()

    return (
        JiraCloudIssueReader(
            make_client(handler), field_map={}, comment_page_size=2, max_comments=10
        ),
        calls,
    )


def _never_started() -> Any:
    async def start(attempt_id: uuid.UUID) -> None:  # pragma: no cover - never reached
        raise AssertionError

    return start


@pytest.mark.parametrize(
    ("response", "code"),
    [(lambda: httpx.Response(400, text="bad"), "jira_response_invalid"),
     (lambda: httpx.Response(401, text="no"), "jira_authentication_failed"),
     (lambda: httpx.Response(403, text="no"), "jira_permission_denied"),
     (lambda: httpx.Response(501, text="no"), "jira_service_unavailable")],
    ids=["http_400", "http_401", "http_403", "http_501"],
)  # fmt: skip
async def test_a_permanent_jira_error_fails_intake_at_once_with_its_fixed_code(
    response: Callable[[], httpx.Response], code: str
) -> None:
    reg, changes = await registry(), MemoryChanges()
    req = changes.add_request()
    jira, calls = _jira_answering(response)
    handler = IntakeHandler(
        changes=changes.service(),
        jira=jira,
        resolver=RepositoryResolver(reg.uow),
        start=_never_started(),
        repository_field_configured=False,
    )
    with pytest.raises(JobFailedError) as failed:
        await handler(job({"code_change_request_id": str(req.id)}))
    assert failed.value.code == code and calls[0] == 1  # read once, never retried
    assert failed.value.__cause__ is None and failed.value.__suppress_context__
    untouched = await changes.service().get_request(req.id)
    assert untouched.repository_id is None and changes.state.attempts == {}


def _timeout() -> httpx.Response:
    raise httpx.ReadTimeout("slow")


@pytest.mark.parametrize(
    ("response", "error"),
    [(lambda: httpx.Response(429, text="slow down"), JiraRateLimitError),
     (lambda: httpx.Response(503, text="down"), JiraServiceUnavailableError),
     (_timeout, JiraTimeoutError)],
    ids=["http_429", "http_503", "timeout"],
)  # fmt: skip
async def test_a_transient_jira_error_propagates_unchanged_so_the_queue_retries_it(
    response: Callable[[], httpx.Response], error: type[Exception]
) -> None:
    reg, changes = await registry(), MemoryChanges()
    req = changes.add_request()
    jira, _ = _jira_answering(response)
    handler = IntakeHandler(
        changes=changes.service(),
        jira=jira,
        resolver=RepositoryResolver(reg.uow),
        start=_never_started(),
        repository_field_configured=False,
    )
    with pytest.raises(error) as raised:
        await handler(job({"code_change_request_id": str(req.id)}))
    assert not isinstance(raised.value, JobFailedError) and raised.value.retryable  # type: ignore[attr-defined]
    untouched = await changes.service().get_request(req.id)
    assert untouched.repository_id is None and changes.state.attempts == {}


@pytest.mark.parametrize(
    "response",
    [lambda: httpx.Response(408, text="timeout"),
     lambda: httpx.Response(425, text="too early"),
     lambda: httpx.Response(200, text="<html>maintenance</html>",
                            headers={"content-type": "text/html"}),
     lambda: httpx.Response(200, text="{not json", headers={"content-type": "application/json"})],
    ids=["http_408", "http_425", "html_page", "malformed_json"],
)  # fmt: skip
async def test_a_usually_transient_jira_response_is_retried_by_the_queue(
    response: Callable[[], httpx.Response],
) -> None:
    reg, changes = await registry(), MemoryChanges()
    req = changes.add_request()
    jira, _ = _jira_answering(response)
    handler = IntakeHandler(
        changes=changes.service(),
        jira=jira,
        resolver=RepositoryResolver(reg.uow),
        start=_never_started(),
        repository_field_configured=False,
    )
    with pytest.raises(JobRetryableError) as raised:
        await handler(job({"code_change_request_id": str(req.id)}))
    assert raised.value.code == "jira_response_invalid"
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    untouched = await changes.service().get_request(req.id)
    assert untouched.repository_id is None and changes.state.attempts == {}


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


def test_composition_passes_the_reference_limits_and_ranking_hosts_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ai_code_change_agent.composition as composition

    made: dict[str, dict[str, Any]] = {}
    real_fetcher = HttpReferenceFetcher

    def fetcher(**kw: Any) -> Any:
        made["fetcher"] = kw
        return real_fetcher(**kw)

    class Searcher:
        allowed_hosts = ("docs.python.org",)

        def __init__(self, **kw: Any) -> None:
            made["search"] = kw

    monkeypatch.setattr(composition, "HttpReferenceFetcher", fetcher)
    monkeypatch.setattr(composition, "AnthropicDocumentSearch", Searcher)
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        "version: 1\nallowed_hosts: [docs.python.org, pypi.org]\nsecondary_hosts: [pypi.org]\n"
    )
    s = settings(
        reference_sources_file=sources,
        llm_provider="anthropic",
        llm_model="claude-test",
        anthropic_api_key="sk-test",
        reference_max_url_chars=120,
        reference_max_redirects=1,
        reference_search_max_hits=4,
    )
    factory = build_reference_tools(s)
    assert factory is not None
    search_tool, fetch_tool = factory("t-1")
    assert made["fetcher"]["max_url_chars"] == 120 and made["fetcher"]["max_redirects"] == 1
    assert made["search"]["max_url_chars"] == 120 and made["search"]["max_hits"] == 4
    assert made["search"]["secondary_hosts"] == ["pypi.org"]
    assert search_tool.name == "search_docs" and fetch_tool.name == "fetch_reference"
    assert "longer than 120 characters" in fetch_tool.description
