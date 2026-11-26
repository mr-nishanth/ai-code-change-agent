"""Architecture guards for the 1 workflow + 1 Deep Agent + 1 validation engine design."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "ai_code_change_agent"
ROOT = SRC.parents[1]


def imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def files(*parts: str) -> list[Path]:
    return sorted(p for p in SRC.joinpath(*parts).rglob("*.py"))


def where(root_module: str) -> set[str]:
    return {
        str(p.relative_to(SRC))
        for p in files()
        if any(i == root_module or i.startswith(root_module + ".") for i in imports(p))
    }


def test_sdk_imports_are_confined() -> None:
    assert where("deepagents") <= {"agents/coding/agent.py", "execution/docker/sandbox.py"}
    assert where("langchain_anthropic") == {"providers/llm/factory.py"}
    for sdk in ("langchain_aws", "boto3", "botocore"):
        assert where(sdk) <= {"providers/llm/factory.py"}, sdk
    assert {p for p in where("langgraph") if not p.startswith("orchestration/")} <= {
        "agents/coding/agent.py"
    }
    assert where("jwt") | where("cryptography") <= {
        "integrations/github/authentication.py",
        "integrations/github/credentials.py",
    }


def test_there_is_exactly_one_agent_and_it_holds_no_credentials() -> None:
    agent_files = files("agents")
    assert {p.parent.name for p in agent_files if p.name == "agent.py"} == {"coding"}
    for p in agent_files:
        for name in imports(p):
            assert not name.startswith(
                (
                    "ai_code_change_agent.integrations",
                    "ai_code_change_agent.persistence",
                    "ai_code_change_agent.core.config",
                    "ai_code_change_agent.composition",
                    "httpx",
                )
            ), (p, name)
    text = (SRC / "agents" / "coding" / "agent.py").read_text()
    # only the SDK's sandbox-backed tools plus the injected harness-side factory (ADR-011)
    assert "tools=list(self._tools(thread_id)) if self._tools else []" in text
    for p in files("agents"):
        assert not any(i.startswith(("httpx", "socket", "requests", "urllib")) for i in imports(p))


def test_the_reference_fetcher_is_the_only_web_client_outside_known_integrations() -> None:
    assert where("ai_code_change_agent.integrations.web") <= {
        "composition.py",
        "integrations/web/fetcher.py",
    }


def test_validation_is_independent_of_any_model() -> None:
    for p in files("validation"):
        for name in imports(p):
            assert not name.startswith(
                (
                    "langchain",
                    "deepagents",
                    "langgraph",
                    "anthropic",
                    "ai_code_change_agent.providers",
                    "ai_code_change_agent.agents.coding.agent",
                )
            ), (p, name)


def test_the_api_never_runs_agents_sandboxes_or_workflows() -> None:
    for p in [*files("api"), SRC / "main.py"]:
        for name in imports(p):
            assert not name.startswith(
                (
                    "ai_code_change_agent.agents",
                    "ai_code_change_agent.orchestration",
                    "ai_code_change_agent.execution",
                    "ai_code_change_agent.validation",
                    "ai_code_change_agent.providers",
                )
            ), (p, name)


def test_no_redis_no_static_allowlist_and_no_legacy_names() -> None:
    env = (ROOT / ".env.example").read_text()
    for banned in (
        "REDIS",
        "ALLOWLIST",
        "APP_DATABASE_URL",
        "APP_GITHUB",
        "OUTBOX",
        "MIGRATION_",
        "GRAPHIFY",
        "JIRA_FIELD_DMS",
    ):
        assert banned not in env, banned
    for p in files():
        text = p.read_text().lower()
        assert "redis" not in text and "allowlist" not in text, p


def test_github_writes_never_merge_force_or_touch_other_verbs() -> None:
    text = (SRC / "integrations" / "github" / "publisher.py").read_text()
    for banned in ('"PATCH"', '"PUT"', '"DELETE"', "/merge", '"force"'):
        assert banned not in text, banned


def test_only_two_processes_have_entry_points() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'ai-code-change = "ai_code_change_agent.cli.main:main"' in pyproject
    assert not (ROOT / "docker" / "worker").exists()  # the worker runs on the host (decision C7)


def test_only_the_anthropic_and_bedrock_providers_exist() -> None:
    for sdk in ("langchain_ollama", "langchain_nvidia_ai_endpoints", "langchain_openai"):
        assert where(sdk) == set(), sdk
