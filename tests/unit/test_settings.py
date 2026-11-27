"""The environment variable contract (Settings)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError


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
    monkeypatch.setenv("APP_DATABASE_URL", "postgres://old-name-is-ignored")
    monkeypatch.setenv("DATABASE_URL", "postgres://no-longer-a-setting")
    loaded = s()
    assert loaded.jira_project_keys == frozenset({"SHOP", "OPS"})
    assert loaded.max_concurrent_jobs == 4
    assert "database_url" not in Settings.model_fields and not hasattr(loaded, "database_url")


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


def test_production_requires_its_core_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL")
    ok = s(
        app_env="production",
        llm_provider="bedrock",
        jira_webhook_secret="x",
        jira_project_keys="SHOP",
    )
    assert ok.jira_webhook_configured and not hasattr(ok, "database_url")  # DynamoDB only


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


# ---- AWS (DynamoDB, SQS, S3) -----------------------------------------------------------------
PROD = {
    "llm_provider": "bedrock",
    "jira_webhook_secret": "x",
    "jira_project_keys": "SHOP",
}


def test_aws_defaults_name_the_local_resources() -> None:
    d = s()
    assert d.aws_endpoint_url == "http://127.0.0.1:4566"  # set by tests/conftest.py
    assert (
        d.aws_region,
        d.dynamodb_table_name,
        d.dynamodb_checkpoint_table_name,
        d.sqs_job_queue_name,
        d.s3_bucket_name,
    ) == (
        "us-east-1",
        "acca-local-app",
        "acca-local-checkpoints",
        "acca-local-jobs",
        "acca-local-artifacts",
    )


def test_aws_settings_use_only_acca_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "ACCA_AWS_REGION": "eu-west-1",
        "ACCA_AWS_ENDPOINT_URL": "http://acca-floci:4566/",
        "ACCA_DYNAMODB_TABLE_NAME": "s2116-app",
        "ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME": "s2116-checkpoints",
        "ACCA_SQS_JOB_QUEUE_NAME": "s2116-jobs",
        "ACCA_S3_BUCKET_NAME": "s2116-artifacts",
        # the standard AWS and unprefixed names never configure the application
        "AWS_REGION": "ap-south-1",
        "AWS_DEFAULT_REGION": "ap-south-1",
        "AWS_ENDPOINT_URL": "http://203.0.113.1:1",
        "AWS_ENDPOINT_URL_DYNAMODB": "http://203.0.113.1:1",
        "DYNAMODB_TABLE_NAME": "wrong",
        "S3_BUCKET_NAME": "wrong",
    }.items():
        monkeypatch.setenv(name, value)
    d = s()
    assert (d.aws_region, d.aws_endpoint_url) == ("eu-west-1", "http://acca-floci:4566")
    assert (d.dynamodb_table_name, d.dynamodb_checkpoint_table_name) == (
        "s2116-app",
        "s2116-checkpoints",
    )
    assert (d.sqs_job_queue_name, d.s3_bucket_name) == ("s2116-jobs", "s2116-artifacts")


@pytest.mark.parametrize("env", ["development", "test"])
@pytest.mark.parametrize("value", [None, "", "  "])
def test_local_environments_require_an_aws_endpoint(
    env: str, value: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if value is None:
        monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL")
    else:
        monkeypatch.setenv("ACCA_AWS_ENDPOINT_URL", value)
    with pytest.raises(ConfigurationError, match="requires ACCA_AWS_ENDPOINT_URL"):
        s(app_env=env)


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize("url", ["http://127.0.0.1:4566", "http://acca-floci:4566"])
def test_deployed_environments_refuse_an_aws_endpoint(
    env: str, url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACCA_AWS_ENDPOINT_URL", url)
    with pytest.raises(ConfigurationError, match="must not set ACCA_AWS_ENDPOINT_URL"):
        s(app_env=env, **PROD)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_environments_use_real_aws(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL")
    assert s(app_env=env, **PROD).aws_endpoint_url is None


@pytest.mark.parametrize("env", ["development", "test"])
@pytest.mark.parametrize(
    "url",
    [
        "http://acca-floci:4566",
        "http://127.0.0.1:4566",
        "http://localhost:4566",
        "http://[::1]:4566",
        "http://127.0.0.1:4577/",
    ],
)
def test_local_endpoints_are_accepted(env: str, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCA_AWS_ENDPOINT_URL", url)
    assert s(app_env=env).aws_endpoint_url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://floci.example.com",
        "https://dynamodb.us-east-1.amazonaws.com",
        "http://dynamodb.us-east-1.amazonaws.com",
        "http://bedrock-runtime.us-east-1.api.aws",
        "http://s3.cn-north-1.amazonaws.com.cn",
        "https://127.0.0.1:4566",
        "http://127.0.0.2:4566",
        "http://10.0.0.5:4566",
        "http://user:pw@127.0.0.1:4566",
        "http://127.0.0.1:4566/path",
        "http://127.0.0.1:4566?x=1",
        "http://127.0.0.1:notaport",
        "ftp://127.0.0.1:4566",
        "acca-floci:4566",
        "http://Floci_Host:4566",
    ],
)
def test_non_local_endpoints_are_refused(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCA_AWS_ENDPOINT_URL", url)
    with pytest.raises(ConfigurationError, match="loopback host or a single-label"):
        s(app_env="test")


@pytest.mark.parametrize(
    "name, value",
    [
        ("ACCA_AWS_REGION", "US-EAST-1"),
        ("ACCA_DYNAMODB_TABLE_NAME", "ab"),
        ("ACCA_DYNAMODB_CHECKPOINT_TABLE_NAME", "bad name"),
        ("ACCA_SQS_JOB_QUEUE_NAME", "jobs.fifo"),
        ("ACCA_S3_BUCKET_NAME", "Upper_Case"),
        ("ACCA_S3_BUCKET_NAME", "a..b"),
    ],
)
def test_aws_resource_names_are_validated(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        s()


def test_job_lease_stays_under_the_sqs_visibility_cap() -> None:
    with pytest.raises(ValidationError):
        s(job_lease_seconds=3601)
    assert s(job_lease_seconds=3600).job_lease_seconds <= 43_200


def test_cost_settings_default_to_the_documented_values() -> None:
    d = s()
    assert (d.job_relay_seconds, d.job_sweep_seconds) == (30.0, 300.0)
    assert d.checkpoint_ttl_days == 30 and d.jira_delivery_retention_days == 30


def test_cost_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOB_RELAY_SECONDS", "10")
    monkeypatch.setenv("JOB_SWEEP_SECONDS", "600")
    monkeypatch.setenv("ACCA_CHECKPOINT_TTL_DAYS", "7")
    monkeypatch.setenv("ACCA_JIRA_DELIVERY_RETENTION_DAYS", "2")
    loaded = s()
    assert (loaded.job_relay_seconds, loaded.job_sweep_seconds) == (10.0, 600.0)
    assert (loaded.checkpoint_ttl_days, loaded.jira_delivery_retention_days) == (7, 2)


@pytest.mark.parametrize(
    "kw",
    [
        {"job_relay_seconds": 0.5},
        {"job_relay_seconds": 3601},
        {"job_sweep_seconds": 9},
        {"job_sweep_seconds": 86_401},
        {"job_relay_seconds": 120, "job_sweep_seconds": 60},  # the sweep must not outpace the relay
        {"ACCA_CHECKPOINT_TTL_DAYS": 0},
        {"ACCA_CHECKPOINT_TTL_DAYS": 366},
        {"ACCA_JIRA_DELIVERY_RETENTION_DAYS": 0},
        {"ACCA_JIRA_DELIVERY_RETENTION_DAYS": 366},
    ],
)
def test_cost_settings_out_of_bounds_fail_fast(kw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        s(**kw)


def test_sweep_may_equal_the_relay_interval() -> None:
    same = s(job_relay_seconds=60, job_sweep_seconds=60)
    assert same.job_relay_seconds == same.job_sweep_seconds == 60
