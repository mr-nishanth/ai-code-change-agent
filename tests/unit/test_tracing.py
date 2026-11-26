"""LangSmith tracing configuration: worker-only export, one destination, no stale legacy names."""

import pytest
from pydantic import ValidationError

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.tracing import configure_tracing


def s(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_tracing_is_off_by_default_and_legacy_destinations_are_removed() -> None:
    env = {"LANGCHAIN_PROJECT": "old", "LANGCHAIN_TRACING_V2": "true", "LANGSMITH_PROJECT": "x"}
    assert configure_tracing(s(), env) is False
    assert env == {"LANGSMITH_TRACING": "false"}


def test_project_based_destination() -> None:
    env: dict[str, str] = {}
    on = configure_tracing(
        s(langsmith_tracing=True, langsmith_api_key="lsv2-test", langsmith_hide_inputs=True), env
    )
    assert on and env["LANGSMITH_TRACING"] == "true" and env["LANGSMITH_API_KEY"] == "lsv2-test"
    assert env["LANGSMITH_PROJECT"] == "ai-code-change-agent" and "LANGSMITH_AGENT_ID" not in env
    assert env["LANGSMITH_HIDE_INPUTS"] == "true" and "LANGSMITH_HIDE_OUTPUTS" not in env


def test_agent_based_destination_with_region_and_workspace() -> None:
    env: dict[str, str] = {}
    configure_tracing(
        s(
            langsmith_tracing=True,
            langsmith_api_key="lsv2-test",
            langsmith_agent_id="agt_123",
            langsmith_agent_environment="development",
            langsmith_endpoint="https://eu.api.smith.langchain.com",
            langsmith_workspace_id="ws-1",
        ),
        env,
    )
    assert env["LANGSMITH_AGENT_ID"] == "agt_123" and "LANGSMITH_PROJECT" not in env
    assert env["LANGSMITH_AGENT_ENVIRONMENT"] == "development"
    assert env["LANGSMITH_ENDPOINT"].startswith("https://eu.") and env["LANGSMITH_WORKSPACE_ID"]


@pytest.mark.parametrize(
    "kw",
    [
        {"langsmith_tracing": True},
        {"langsmith_agent_id": "a", "langsmith_project": "p"},
        {"langsmith_endpoint": "http://insecure.example"},
    ],
)
def test_incoherent_tracing_configuration_fails_fast(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        s(**kw)


def test_the_api_key_is_never_rendered() -> None:
    assert "lsv2-secret" not in repr(s(langsmith_tracing=True, langsmith_api_key="lsv2-secret"))
