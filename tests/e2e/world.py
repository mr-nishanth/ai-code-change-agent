"""The local end-to-end world: everything the real composition talks to outside DynamoDB, SQS, S3
and Docker, replaced by deterministic fakes (no network, no real model).

`install` swaps exactly these composition builders, so `worker_app` itself (tables check, checkpointer,
job queue, coordinator, steps, graph, handlers, worker) runs unchanged:

- `build_jira_reader`: the real `JiraCloudIssueReader` over a mock transport serving one synthetic Story;
- `build_baseline_provider` / `build_delivery_publishers`: the real GitHub reader and publisher over
  `FakeGitHub` (a real bare Git repository on disk);
- `build_chat_model`: a scripted tool-calling model; its ledger rows carry provider `fake`
  (`UsageTracker`), so they can never be mistaken for spend;
- `build_job_queue`: the real `DynamoSqsJobQueue`, with a 1 s long poll and a 2 s sweep grace so the
  tests do not wait minutes.

The same `install` runs in the SIGKILL subprocess (`tests/e2e/crash_worker.py`)."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from langchain_core.messages import AIMessage

from ai_code_change_agent import composition
from ai_code_change_agent.application.costs.usage import UsageTracker
from ai_code_change_agent.application.git_providers.baseline import BaselineReaders
from ai_code_change_agent.application.git_providers.delivery import DeliveryPublishers
from ai_code_change_agent.application.workspaces.registry_baseline import RegistryBaselineProvider
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.integrations.github.baseline import GitHubBaselineReader
from ai_code_change_agent.integrations.github.publisher import GitHubAppPublisher
from ai_code_change_agent.integrations.jira.issues import JiraCloudIssueReader
from ai_code_change_agent.persistence.dynamodb.job_queue import DynamoSqsJobQueue
from tests.support import jira_rest
from tests.support.fake_github import FakeGitHub
from tests.support.jira import EVENT, SECRET
from tests.support.models import ScriptedChatModel

IMAGE_DEFAULT = "ai-code-change-agent/execution-python:0.2.0"
ISSUE_KEY = "SHOP-1"
PROJECT = "SHOP"
REPO_ID = 424242
LEDGER_PROVIDER = "fake"
MODEL = "fake-scripted"
REPO_FILES: dict[str, bytes] = {
    "README.md": b"# Shop service\n",
    "app/__init__.py": b"",
    "app/greeting.py": b"def greet():\n    return 'Hello'\n",
    "tests/test_greeting.py": (
        b"from app.greeting import greet\n\n\ndef test_greet():\n    assert greet() == 'Goodbye'\n"
    ),
}
CHANGED = b"def greet():\n    return 'Goodbye'\n"
PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "repository_key": "shop-service",
    "languages": ["Python"],
    "toolchains": ["python"],
    "capabilities": ["test_run"],
    "command_policy": "default-v1",
    "validation_policy": "standard",
    "commands": [
        {"id": "test", "kind": "test", "toolchain": "python", "network": "none",
         "argv": ["python", "-m", "pytest", "-q"], "timeout_seconds": 120},
    ],
}  # fmt: skip
PLAN: dict[str, Any] = {
    "summary": "Return Goodbye",
    "acceptance_criteria": ["greet returns Goodbye"],
    "changes": [{"path": "app/greeting.py", "operation": "modify", "purpose": "new greeting"}],
    "tests": ["tests/test_greeting.py"],
}
REPORT: dict[str, Any] = {
    "outcome": "implemented",
    "summary": "greet returns Goodbye",
    "changed_files": ["app/greeting.py"],
}
# Over DynamoDBSaver's 358,400-byte payload threshold once serialised: the planning run's first
# model turn carries it, so the agent's checkpoint must be offloaded to S3 (pseudo-random hex, not
# compressible). Generated, never read from anywhere.
BIG_BYTES = 450_000
BIG = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(BIG_BYTES // 64 + 1))[
    :BIG_BYTES
]
# Fast worker timings for tests (model_copy skips validation on purpose: production minimums are
# 30 s lease and 10 s sweep, which would make the crash and sweep tests wait minutes).
FAST: dict[str, Any] = {
    "job_lease_seconds": 10,
    "job_heartbeat_seconds": 2,
    "job_relay_seconds": 0.5,
    "job_sweep_seconds": 1.0,
}


def settings_document(
    *,
    endpoint: str,
    table: str,
    checkpoint_table: str,
    queue_name: str,
    bucket: str,
    root: Path,
    key_path: Path,
    image: str,
) -> dict[str, Any]:
    """JSON-safe Settings input (the crash subprocess rebuilds the same Settings from it)."""
    return {
        "app_env": "test",
        "log_json": True,
        "ACCA_AWS_ENDPOINT_URL": endpoint,
        "ACCA_DYNAMODB_TABLE_NAME": table,
        "ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME": checkpoint_table,
        "ACCA_SQS_JOB_QUEUE_NAME": queue_name,
        "ACCA_S3_BUCKET_NAME": bucket,
        "jira_webhook_secret": SECRET,
        "jira_webhook_event": EVENT,
        "jira_project_keys": PROJECT,
        "jira_story_issue_types": "Story",
        "jira_base_url": jira_rest.BASE_URL,
        "jira_email": jira_rest.EMAIL,
        "jira_api_token": jira_rest.TOKEN,
        "github_app_id": 11,
        "github_app_installation_id": 22,
        "github_app_private_key_path": str(key_path),
        "github_pr_create_enabled": True,
        "code_change_delivery_enabled": True,
        "workspace_root": str(root / "workspaces"),
        "artifact_root": str(root / "artifacts"),
        "workspace_image": image,
        "workflow_approval_mode": "manual",
        "llm_provider": "anthropic",
        "llm_model": MODEL,
        "anthropic_api_key": "sk-test-never-used",
        "llm_context_tokens": 2_000_000,
        "reference_search_enabled": False,
        "max_concurrent_jobs": 1,
    }


def make_settings(document: dict[str, Any], fast: dict[str, Any] | None = None) -> Settings:
    settings = Settings.model_validate(document)
    return settings.model_copy(update=FAST if fast is None else fast)


def reply(message: AIMessage, n: int) -> AIMessage:
    """A scripted turn with provider-style usage, so the ledger records it."""
    message.response_metadata = {"id": f"msg_fake_{n:04d}", "model": MODEL}
    message.usage_metadata = {
        "input_tokens": 100 + n,
        "output_tokens": 10 + n,
        "total_tokens": 110 + 2 * n,
    }
    return message


def tool_turn(name: str, args: dict[str, Any], n: int, content: str = "") -> AIMessage:
    return reply(
        AIMessage(content=content, tool_calls=[{"name": name, "args": args, "id": f"call_{n}"}]), n
    )


def planning_script() -> list[AIMessage]:
    return [
        tool_turn("read_file", {"file_path": "/workspace/app/greeting.py"}, 1, content=BIG),
        tool_turn("ImplementationPlan", PLAN, 2),
    ]


def implementation_script() -> list[AIMessage]:
    return [
        tool_turn(
            "edit_file",
            {
                "file_path": "/workspace/app/greeting.py",
                "old_string": "'Hello'",
                "new_string": "'Goodbye'",
            },
            3,
        ),
        tool_turn("ImplementationReport", REPORT, 4),
    ]


@dataclass
class FakeJira:
    """Serves one synthetic Story through the real Jira client (mock transport)."""

    calls: list[str] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if request.url.path == f"/rest/api/3/issue/{ISSUE_KEY}":
            return jira_rest.json_response(
                jira_rest.issue_payload(
                    ISSUE_KEY,
                    "10001",
                    summary="Say goodbye",
                    description=jira_rest.adf(
                        "Change the greeting.", "Acceptance criteria:", "- greet returns Goodbye"
                    ),
                    project={"id": "100", "key": PROJECT, "name": "Shop"},
                )
            )
        return jira_rest.json_response({"errorMessages": ["not found"]}, status=404)


@dataclass
class World:
    settings: Settings
    hub: FakeGitHub
    jira: FakeJira
    model: ScriptedChatModel


def public_key(key_path: Path) -> rsa.RSAPublicKey:
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    return key.public_key()


def build_world(settings: Settings, bare: Path, model: ScriptedChatModel) -> World:
    assert settings.github_app_private_key_path is not None
    key = public_key(Path(settings.github_app_private_key_path))
    hub = FakeGitHub(bare, public_key=key, repo_id=REPO_ID)
    model.profile = {"max_input_tokens": settings.llm_context_tokens}
    return World(settings, hub, FakeJira(), model)


def install(world: World, setattr_: Callable[[Any, str, Any], None]) -> None:
    """Swap the composition builders listed in the module docstring (pytest's monkeypatch.setattr in
    tests, builtin setattr in the crash subprocess)."""
    hub = world.hub

    def jira_reader(settings: Settings) -> JiraCloudIssueReader:
        return JiraCloudIssueReader(
            jira_rest.make_client(world.jira.handle),
            field_map={},
            comment_page_size=settings.jira_comment_page_size,
            max_comments=settings.jira_max_comments,
        )

    def baselines(registry: Any, auth: Any) -> RegistryBaselineProvider:
        reader = GitHubBaselineReader(auth=auth, transport=hub.transport())
        return RegistryBaselineProvider(registry, BaselineReaders({"github": reader}))

    def publishers(settings: Settings, auth: Any) -> DeliveryPublishers:
        assert settings.github_pr_create_enabled and auth is not None
        return DeliveryPublishers(
            {"github": GitHubAppPublisher(auth=auth, transport=hub.transport())}
        )

    def job_queue(settings: Settings, aws: Any) -> DynamoSqsJobQueue:
        return DynamoSqsJobQueue(
            composition.app_table(settings, aws),
            aws.sqs,
            aws.queue_url,
            receive_wait_seconds=1,
            sweep_grace_seconds=2,
        )

    def usage_tracker(*args: Any, **kwargs: Any) -> UsageTracker:
        return UsageTracker(*args, **{**kwargs, "provider": LEDGER_PROVIDER})

    setattr_(composition, "build_jira_reader", jira_reader)
    setattr_(composition, "build_baseline_provider", baselines)
    setattr_(composition, "build_delivery_publishers", publishers)
    setattr_(composition, "build_chat_model", lambda _settings: world.model)
    setattr_(composition, "build_job_queue", job_queue)
    setattr_(composition, "UsageTracker", usage_tracker)


def dump(document: dict[str, Any], bare: Path, path: Path) -> Path:
    path.write_text(json.dumps({"settings": document, "bare": str(bare)}))
    return path
