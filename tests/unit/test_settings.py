"""The environment variable contract (Settings)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_code_change_agent.core.config import Settings


def s(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_defaults_are_safe() -> None:
    d = s()
    assert d.app_env == "development" and d.workspace_network_policy == "none"
    assert not (
        d.github_pr_create_enabled or d.code_change_delivery_enabled or d.jira_reporting_enabled
    )
    assert not d.github_app_configured and not d.jira_rest_configured and not d.llm_configured
    assert d.jira_story_issue_types == frozenset({"Story"})


def test_unprefixed_names_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_PROJECT_KEYS", "SHOP, OPS")
    monkeypatch.setenv("MAX_CONCURRENT_JOBS", "4")
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql://old-name-is-ignored")
    loaded = s()
    assert loaded.jira_project_keys == frozenset({"SHOP", "OPS"})
    assert loaded.max_concurrent_jobs == 4 and loaded.database_url is None


@pytest.mark.parametrize(
    "kw",
    [
        {"github_app_id": 1},
        {"github_pr_create_enabled": True},
        {"jira_base_url": "https://x.atlassian.net"},
        {"jira_reporting_enabled": True},
        {"jira_base_url": "http://x.atlassian.net", "jira_email": "a@b", "jira_api_token": "t"},
        {
            "jira_base_url": "https://u:p@x.atlassian.net/path",
            "jira_email": "a",
            "jira_api_token": "t",
        },
        {"jira_project_keys": "shop"},
        {"workspace_root": "relative/dir"},
        {"artifact_root": "/a/../b"},
        {"workspace_image": "img; rm -rf /"},
        {"langgraph_checkpoint_schema": "public"},
        {"job_lease_seconds": 60, "job_heartbeat_seconds": 40},
        {"max_concurrent_jobs": 0},
        {"workspace_network_policy": "bridge"},
        {"llm_provider": "openai"},
        {"app_env": "production"},
    ],
)
def test_invalid_or_incomplete_configuration_fails_fast(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        s(**kw)


def test_secrets_are_never_rendered(tmp_path: Path) -> None:
    loaded = s(
        jira_base_url="https://x.atlassian.net",
        jira_email="a@b.c",
        jira_api_token="token-value-123",
        anthropic_api_key="sk-ant-secret",
        jira_webhook_secret="hook-secret",
    )
    text = repr(loaded) + str(loaded.model_dump())
    for secret in ("token-value-123", "sk-ant-secret", "hook-secret"):
        assert secret not in text
    assert loaded.jira_rest_configured


def test_production_requires_its_core_settings() -> None:
    ok = s(
        app_env="production",
        llm_provider="bedrock",
        database_url="postgresql+psycopg://u:p@h/db",
        jira_webhook_secret="x",
        jira_project_keys="SHOP",
    )
    assert ok.jira_webhook_configured


def test_reference_limits_default_to_the_documented_values() -> None:
    d = s()
    assert (d.reference_max_url_chars, d.reference_max_redirects, d.reference_search_max_hits) == (
        300,
        3,
        8,
    )


@pytest.mark.parametrize(
    "kw",
    [
        {"reference_max_url_chars": 49},
        {"reference_max_url_chars": 2001},
        {"reference_max_redirects": -1},
        {"reference_max_redirects": 11},
        {"reference_search_max_hits": 0},
        {"reference_search_max_hits": 21},
    ],
)
def test_reference_limits_out_of_bounds_fail_fast(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        s(**kw)


def test_reference_limits_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFERENCE_MAX_URL_CHARS", "120")
    monkeypatch.setenv("REFERENCE_MAX_REDIRECTS", "0")
    monkeypatch.setenv("REFERENCE_SEARCH_MAX_HITS", "4")
    d = s()
    assert (d.reference_max_url_chars, d.reference_max_redirects, d.reference_search_max_hits) == (
        120,
        0,
        4,
    )
