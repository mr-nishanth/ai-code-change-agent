"""Composition root: the only place that wires adapters to ports. Built once per process."""

import contextlib
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ai_code_change_agent.agents.coding.agent import CodingAgent
from ai_code_change_agent.agents.coding.configuration import CodingAgentConfig
from ai_code_change_agent.agents.coding.tools import build_reference_tool, build_search_tool
from ai_code_change_agent.application.artifacts.service import ArtifactService
from ai_code_change_agent.application.changes.service import CodeChangeService
from ai_code_change_agent.application.costs.pricing import PriceTable
from ai_code_change_agent.application.costs.report import totals
from ai_code_change_agent.application.costs.service import CostReporter
from ai_code_change_agent.application.costs.usage import UsageTracker
from ai_code_change_agent.application.git_providers.baseline import BaselineReaders
from ai_code_change_agent.application.git_providers.delivery import DeliveryPublishers
from ai_code_change_agent.application.git_providers.ports import GitProviderRegistry
from ai_code_change_agent.application.jobs.worker import Worker
from ai_code_change_agent.application.multirepo.coordinator import Coordinator
from ai_code_change_agent.application.references.ports import SearchResult
from ai_code_change_agent.application.repositories.ports import RegistryUnitOfWork
from ai_code_change_agent.application.repositories.profile_reader import ProfileReader
from ai_code_change_agent.application.repositories.resolver import RepositoryResolver
from ai_code_change_agent.application.repositories.service import RepositoryRegistryService
from ai_code_change_agent.application.webhooks.receive import ReceiveJiraWebhook
from ai_code_change_agent.application.workspaces.registry_baseline import RegistryBaselineProvider
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.core.logging import get_logger
from ai_code_change_agent.core.tracing import configure_tracing, flush_traces
from ai_code_change_agent.domain.repositories.command_policy import CommandPolicy
from ai_code_change_agent.domain.webhooks.policy import ScopePolicy
from ai_code_change_agent.execution.artifacts import LocalFilesystemArtifactStore
from ai_code_change_agent.execution.docker.dependencies import DockerDependencyPreparer
from ai_code_change_agent.execution.docker.metadata_verifier import MetadataVerifier
from ai_code_change_agent.execution.docker.process import SubprocessRunner
from ai_code_change_agent.execution.docker.resolver_network import ResolverEgress, egress_hosts
from ai_code_change_agent.execution.docker.runtime import ContainerLimits, DockerWorkspaceRuntime
from ai_code_change_agent.execution.docker.sandbox import SandboxLimits, SandboxManager
from ai_code_change_agent.integrations.github.authentication import GitHubAppAuth
from ai_code_change_agent.integrations.github.baseline import PROVIDER_KEY as GITHUB_KEY
from ai_code_change_agent.integrations.github.baseline import GitHubBaselineReader
from ai_code_change_agent.integrations.github.credentials import load_credentials
from ai_code_change_agent.integrations.github.publisher import GitHubAppPublisher
from ai_code_change_agent.integrations.github.references import GitHubReferenceParser
from ai_code_change_agent.integrations.github.repositories import GitHubAppRepositoryClient
from ai_code_change_agent.integrations.jira.authentication import BasicAuthenticator
from ai_code_change_agent.integrations.jira.client import JiraClientConfig, JiraHttpClient
from ai_code_change_agent.integrations.jira.issues import JiraCloudIssueReader
from ai_code_change_agent.integrations.jira.reporting import JiraOutcomeReporter
from ai_code_change_agent.integrations.jira.webhook import JiraCloudSignatureVerifier
from ai_code_change_agent.integrations.web.fetcher import HttpReferenceFetcher
from ai_code_change_agent.integrations.web.search import AnthropicDocumentSearch
from ai_code_change_agent.orchestration.intake import (
    CHILD,
    INTAKE,
    PARENT_REPORT,
    RESUME,
    ChildHandler,
    IntakeHandler,
    ParentReportHandler,
    ResumeHandler,
)
from ai_code_change_agent.orchestration.langgraph.checkpointer import postgres_checkpointer
from ai_code_change_agent.orchestration.langgraph.graph import build_graph
from ai_code_change_agent.orchestration.langgraph.runner import WorkflowRunner
from ai_code_change_agent.orchestration.steps import CodeChangeSteps, StepDependencies
from ai_code_change_agent.persistence.repositories.change_store import SqlChangeUnitOfWork
from ai_code_change_agent.persistence.repositories.job_queue import PostgresJobQueue
from ai_code_change_agent.persistence.repositories.multirepo_store import PostgresMultiRepoStore
from ai_code_change_agent.persistence.repositories.registry_store import (
    SqlAlchemyRegistryUnitOfWork,
)
from ai_code_change_agent.persistence.repositories.usage_store import PostgresUsageStore
from ai_code_change_agent.persistence.repositories.webhook_repository import (
    SqlAlchemyWebhookRepository,
)
from ai_code_change_agent.persistence.session import create_engine, create_session_factory
from ai_code_change_agent.providers.llm.factory import build_chat_model

_log = get_logger(__name__)


def require_database(settings: Settings) -> str:
    if settings.database_url is None:
        msg = "DATABASE_URL is required"
        raise ConfigurationError(msg)
    return settings.database_url.get_secret_value()


# ---- Jira -----------------------------------------------------------------------------------
def build_webhook_service(
    settings: Settings, session_factory: async_sessionmaker[AsyncSession]
) -> ReceiveJiraWebhook:
    if settings.jira_webhook_secret is None:
        msg = "JIRA_WEBHOOK_SECRET is required for the webhook"
        raise ConfigurationError(msg)
    return ReceiveJiraWebhook(
        verifier=JiraCloudSignatureVerifier(settings.jira_webhook_secret.get_secret_value()),
        repository=SqlAlchemyWebhookRepository(
            session_factory, job_max_attempts=settings.job_max_attempts
        ),
        policy=ScopePolicy(
            supported_event=settings.jira_webhook_event,
            allowed_project_keys=settings.jira_project_keys,
            allowed_issue_types=settings.jira_story_issue_types,
        ),
    )


def _jira_authenticator(settings: Settings) -> BasicAuthenticator | None:
    if not settings.jira_rest_configured:
        return None
    assert settings.jira_email is not None and settings.jira_api_token is not None  # noqa: S101
    return BasicAuthenticator(settings.jira_email, settings.jira_api_token)


def build_jira_reader(settings: Settings) -> JiraCloudIssueReader | None:
    """Read-only Jira REST reader, or None without JIRA_BASE_URL/EMAIL/API_TOKEN."""
    auth = _jira_authenticator(settings)
    if auth is None:
        return None
    assert settings.jira_base_url is not None  # noqa: S101 - guarded by jira_rest_configured
    client = JiraHttpClient(
        JiraClientConfig(
            base_url=settings.jira_base_url,
            connect_timeout_seconds=settings.jira_connect_timeout_seconds,
            read_timeout_seconds=settings.jira_read_timeout_seconds,
            max_retries=settings.jira_max_retries,
            max_response_bytes=settings.jira_max_response_bytes,
        ),
        auth,
    )
    field_map = (
        {"repository": settings.jira_repository_field} if settings.jira_repository_field else {}
    )
    return JiraCloudIssueReader(
        client,
        field_map=field_map,
        comment_page_size=settings.jira_comment_page_size,
        max_comments=settings.jira_max_comments,
    )


def build_jira_reporter(settings: Settings) -> JiraOutcomeReporter:
    """Disabled unless JIRA_REPORTING_ENABLED=true (Settings also requires the REST config)."""
    return JiraOutcomeReporter(
        enabled=settings.jira_reporting_enabled,
        base_url=settings.jira_base_url,
        authenticator=_jira_authenticator(settings),
        timeout_seconds=settings.jira_read_timeout_seconds,
    )


# ---- registry and GitHub ------------------------------------------------------------------
def registry_uow_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], RegistryUnitOfWork]:
    return lambda: SqlAlchemyRegistryUnitOfWork(session_factory)


def build_registry_service(
    session_factory: async_sessionmaker[AsyncSession],
    providers: GitProviderRegistry | None = None,
) -> RepositoryRegistryService:
    return RepositoryRegistryService(registry_uow_factory(session_factory), providers=providers)


def build_repository_resolver(
    session_factory: async_sessionmaker[AsyncSession],
) -> RepositoryResolver:
    return RepositoryResolver(
        registry_uow_factory(session_factory), parsers=(GitHubReferenceParser(),)
    )


def build_github_auth(settings: Settings) -> GitHubAppAuth | None:
    """The one App authentication object of this process (shared by baseline reader,
    registration client and publisher), or None without App configuration."""
    if not settings.github_app_configured:
        return None
    return GitHubAppAuth(
        load_credentials(
            settings.github_app_id,
            settings.github_app_installation_id,
            settings.github_app_private_key_path,
        )
    )


def build_git_provider_clients(github_auth: GitHubAppAuth | None) -> GitProviderRegistry:
    providers = GitProviderRegistry()
    if github_auth is not None:
        providers.register(GITHUB_KEY, GitHubAppRepositoryClient(auth=github_auth))
    return providers


def build_baseline_provider(
    registry_uow: Callable[[], RegistryUnitOfWork], github_auth: GitHubAppAuth | None
) -> RegistryBaselineProvider:
    readers = BaselineReaders()
    if github_auth is not None:
        readers.register(GITHUB_KEY, GitHubBaselineReader(auth=github_auth))
    return RegistryBaselineProvider(registry_uow, readers)


def build_delivery_publishers(
    settings: Settings, github_auth: GitHubAppAuth | None
) -> DeliveryPublishers:
    """The GitHub publisher is registered only with GITHUB_PR_CREATE_ENABLED (and the App)."""
    publishers = DeliveryPublishers()
    if settings.github_pr_create_enabled and github_auth is not None:
        publishers.register(GITHUB_KEY, GitHubAppPublisher(auth=github_auth))
    return publishers


# ---- execution ---------------------------------------------------------------------------
def build_artifact_service(settings: Settings) -> ArtifactService:
    if settings.artifact_root is None:
        msg = "ARTIFACT_ROOT is required"
        raise ConfigurationError(msg)
    store = LocalFilesystemArtifactStore(
        Path(settings.artifact_root), max_artifact_bytes=settings.artifact_max_bytes
    )
    return ArtifactService(
        store,
        max_artifact_bytes=settings.artifact_max_bytes,
        max_attempt_bytes=settings.artifact_max_attempt_bytes,
    )


def _image(settings: Settings) -> str:
    if settings.workspace_root is None or settings.workspace_image is None:
        msg = "WORKSPACE_ROOT and WORKSPACE_IMAGE are required for execution"
        raise ConfigurationError(msg)
    return settings.workspace_image


def build_workspace_runtime(settings: Settings) -> DockerWorkspaceRuntime:
    images = execution_images(settings)  # fails fast without WORKSPACE_ROOT/WORKSPACE_IMAGE
    assert settings.workspace_root is not None  # noqa: S101
    return DockerWorkspaceRuntime(
        root=Path(settings.workspace_root),
        images=images,
        policy=CommandPolicy(),
        limits=ContainerLimits(
            memory_mb=settings.workspace_memory_limit_mb,
            cpus=settings.workspace_cpu_limit,
            pids=settings.workspace_pids_limit,
            max_workspace_bytes=settings.workspace_disk_limit_mb * 1024 * 1024,
        ),
        dependency_root=dependency_root(settings) if settings.artifact_root else None,
    )


def dependency_root(settings: Settings) -> Path:
    if settings.artifact_root is None:
        msg = "ARTIFACT_ROOT is required for dependency bundles"
        raise ConfigurationError(msg)
    return Path(settings.artifact_root) / "dependency-bundles"


def build_dependency_preparer(settings: Settings) -> DockerDependencyPreparer:
    runner = SubprocessRunner()
    return DockerDependencyPreparer(
        root=dependency_root(settings),
        image=_image(settings),
        index_url=settings.dependency_index_url,
        runner=runner,
        egress=ResolverEgress(
            runner=runner,
            image=_image(settings),
            allow_hosts=egress_hosts(settings.dependency_index_url),
            network=settings.dependency_resolver_network,
            lifetime_seconds=settings.dependency_timeout_seconds + 120,
        ),
        verifier=MetadataVerifier(
            settings.dependency_index_url,
            allow_unverified_index=settings.dependency_allow_unverified_index,
        ),
        max_packages=settings.dependency_max_packages,
        max_bytes=settings.dependency_max_bytes,
        timeout_seconds=settings.dependency_timeout_seconds,
    )


def build_reference_tools(
    settings: Settings, usage: UsageTracker | None = None
) -> Callable[[str], list[BaseTool]] | None:
    """The agent's harness-side research tools (search_docs when the Anthropic provider and
    REFERENCE_SEARCH_ENABLED allow it, and fetch_reference), or None when no domain is
    allow-listed. Built afresh per phase run (per-phase budgets); searches are costed."""
    if not settings.reference_allowed_domains:
        return None
    domains = sorted(settings.reference_allowed_domains)
    fetcher = HttpReferenceFetcher(
        allowed_domains=domains,
        max_bytes=settings.reference_max_bytes,
        timeout_seconds=settings.reference_timeout_seconds,
    )
    searcher = None
    if (
        settings.reference_search_enabled
        and settings.llm_provider == "anthropic"
        and settings.anthropic_api_key is not None
        and settings.llm_model
    ):
        searcher = AnthropicDocumentSearch(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.llm_model,
            allowed_domains=domains,
        )

    def tools(thread_id: str) -> list[BaseTool]:
        out = [build_reference_tool(fetcher, max_calls=settings.reference_max_fetches)]
        if searcher is not None:

            async def record(result: SearchResult) -> None:
                if usage is not None:
                    await usage.record_search(thread_id, result)

            out.insert(
                0,
                build_search_tool(
                    searcher, max_calls=settings.reference_max_searches, on_result=record
                ),
            )
        return out

    return tools


def execution_images(settings: Settings) -> dict[str, str]:
    """Toolchain family -> execution image. Repository execution is Python only, so the single
    entry is WORKSPACE_IMAGE; a profile declaring another toolchain is BLOCKED before any work."""
    return {"python": _image(settings)}


def build_sandbox_manager(settings: Settings) -> SandboxManager:
    return SandboxManager(
        image=_image(settings),
        images=execution_images(settings),
        limits=SandboxLimits(
            cpus=settings.workspace_cpu_limit,
            memory_mb=settings.workspace_memory_limit_mb,
            pids=settings.workspace_pids_limit,
            disk_bytes=settings.workspace_disk_limit_mb * 1024 * 1024,
            command_timeout_seconds=settings.deep_agent_execute_timeout_seconds,
            lifetime_seconds=settings.workspace_timeout_seconds,
        ),
        runner=SubprocessRunner(),
    )


# ---- worker ---------------------------------------------------------------------------------
@dataclass
class WorkerApp:
    worker: Worker
    runner: WorkflowRunner
    changes: CodeChangeService
    costs: CostReporter


def build_price_table(settings: Settings) -> PriceTable:
    return (
        PriceTable.from_file(settings.llm_pricing_file)
        if settings.llm_pricing_file
        else PriceTable()
    )


def build_cost_reporter(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    changes: CodeChangeService,
    session_id: uuid.UUID,
    write: Callable[[str], Any] = sys.stdout.write,
) -> CostReporter:
    async def status_of(attempt_id: uuid.UUID) -> tuple[str, bool]:
        attempt = await changes.get_attempt(attempt_id)
        return attempt.status.value, attempt.terminal

    def flushed(text: str) -> None:
        write(text)
        with contextlib.suppress(Exception):
            sys.stdout.flush()

    return CostReporter(
        PostgresUsageStore(sessions),
        build_price_table(settings),
        provider=settings.llm_provider,
        primary_model=settings.llm_model or "-",
        session_id=session_id,
        status_of=status_of,
        write=flushed,
    )


def build_change_service(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> CodeChangeService:
    return CodeChangeService(
        lambda: SqlChangeUnitOfWork(session_factory), max_repairs=settings.max_repair_attempts
    )


@asynccontextmanager
async def worker_app(settings: Settings) -> AsyncIterator[WorkerApp]:
    """Everything the worker needs, built once. Fails fast on missing configuration."""
    tracing = configure_tracing(settings)  # LangSmith: worker only, off unless configured
    url = require_database(settings)
    jira = build_jira_reader(settings)
    if jira is None:
        msg = "JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN are required for the worker"
        raise ConfigurationError(msg)
    auth = build_github_auth(settings)
    if auth is None:
        msg = "the GitHub App configuration is required for the worker"
        raise ConfigurationError(msg)
    engine: AsyncEngine = create_engine(url)
    sessions = create_session_factory(engine)
    registry = registry_uow_factory(sessions)
    changes = build_change_service(sessions, settings)
    runtime = build_workspace_runtime(settings)
    costs = build_cost_reporter(sessions, settings, changes, uuid.uuid4())
    prices = build_price_table(settings)
    if settings.llm_model and not prices.knows(settings.llm_model):
        _log.warning(
            "llm_model_unpriced",
            model=settings.llm_model,
            note="costs are reported as unknown until LLM_PRICING_FILE prices this model; the "
            "phase budget uses the matching Claude list price when one exists",
        )
    usage = UsageTracker(
        PostgresUsageStore(sessions),
        build_price_table(settings),
        provider=settings.llm_provider,
        model=settings.llm_model or "-",
        session_id=costs.session_id,
        phase_budget_usd=Decimal(str(settings.llm_max_cost_per_phase_usd)),
    )
    async with postgres_checkpointer(url, settings.langgraph_checkpoint_schema) as saver:
        artifacts = build_artifact_service(settings)
        reporter = build_jira_reporter(settings)
        resolver = build_repository_resolver(sessions)
        coordinator = build_coordinator(sessions, settings, changes, artifacts, resolver, reporter)
        deps = StepDependencies(
            changes=changes,
            registry_uow=registry,
            profiles=ProfileReader(registry),
            baselines=build_baseline_provider(registry, auth),
            runtime=runtime,
            sandboxes=build_sandbox_manager(settings),
            agent=CodingAgent(
                build_chat_model(settings),
                CodingAgentConfig(
                    recursion_limit=settings.deep_agent_recursion_limit,
                    run_timeout_seconds=settings.deep_agent_run_timeout_seconds,
                    clear_tool_outputs_tokens=settings.llm_context_tokens // 2,
                    max_model_calls=settings.deep_agent_max_model_calls,
                    max_phase_cost_usd=settings.llm_max_cost_per_phase_usd,
                ),
                build_reference_tools(settings, usage),
                usage.handlers,
            ),
            engine=_engine(runtime),
            artifacts=artifacts,
            jira=jira,
            publishers=build_delivery_publishers(settings, auth),
            reporter=reporter,
            delivery_enabled=settings.code_change_delivery_enabled,
            pr_create_enabled=settings.github_pr_create_enabled,
            max_repairs=settings.max_repair_attempts,
            agent_checkpointer=saver,
            dependencies=build_dependency_preparer(settings),
            approval_mode=settings.workflow_approval_mode,
            max_dependency_packages=settings.dependency_max_packages,
            dependency_index_url=settings.dependency_index_url,
            multirepo=coordinator,
            toolchains=frozenset(execution_images(settings)),
        )
        runner = WorkflowRunner(build_graph(CodeChangeSteps(deps), saver))
        handlers: dict[str, Any] = {
            INTAKE: IntakeHandler(
                changes=changes,
                jira=jira,
                resolver=resolver,
                start=costs.around(runner.start),
                repository_field_configured=settings.jira_repository_field is not None,
                reporter=deps.reporter,
                coordinator=coordinator,
            ),
            RESUME: ResumeHandler(costs.around(runner.resume)),
            CHILD: ChildHandler(
                coordinator=coordinator, changes=changes, start=costs.around(runner.start)
            ),
            PARENT_REPORT: ParentReportHandler(coordinator=coordinator),
        }
        worker = Worker(
            PostgresJobQueue(sessions),
            handlers,
            lease_seconds=settings.job_lease_seconds,
            heartbeat_seconds=settings.job_heartbeat_seconds,
            concurrency=settings.max_concurrent_jobs,
        )
        try:
            yield WorkerApp(worker, runner, changes, costs)
        finally:
            await jira.aclose()
            if tracing:
                flush_traces()
            await engine.dispose()


def build_coordinator(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    changes: CodeChangeService,
    artifacts: Any,
    resolver: RepositoryResolver,
    reporter: Any,
) -> Coordinator:
    usage = PostgresUsageStore(sessions)

    async def cost_of_issue(issue_key: str) -> str:
        return totals(r.record for r in await usage.rows(issue_keys=[issue_key])).money()

    return Coordinator(
        store=PostgresMultiRepoStore(sessions),
        changes=changes,
        queue=PostgresJobQueue(sessions),
        artifacts=artifacts,
        resolver=resolver,
        reporter=reporter,
        max_repositories=settings.multi_repo_max_repositories,
        max_job_attempts=settings.job_max_attempts,
        cost_of_issue=cost_of_issue,
    )


def _engine(runtime: DockerWorkspaceRuntime) -> Any:
    from ai_code_change_agent.validation.engine import ValidationEngine

    return ValidationEngine(runtime)
