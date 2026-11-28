"""LLM provider selection: Anthropic for local development and tests, Bedrock for staging and
production (the only two providers)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.providers.llm.factory import build_chat_model


def s(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


PROD = {
    "jira_webhook_secret": "s" * 32,
    "jira_project_keys": "SHOP",
}


def test_anthropic_builds_the_claude_api_model_without_a_network_call() -> None:
    settings = s(
        llm_model="claude-haiku-5-5", anthropic_api_key="sk-test", llm_max_output_tokens=512
    )
    assert settings.llm_provider == "anthropic" and settings.llm_configured
    model = build_chat_model(settings)
    assert model.__class__.__name__ == "ChatAnthropic"
    assert model.model == "claude-haiku-5-5"  # type: ignore[attr-defined]
    assert model.max_tokens == 512  # type: ignore[attr-defined]
    assert "sk-test" not in repr(settings)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_anthropic_is_refused_in_staging_and_production(env: str) -> None:
    with pytest.raises(ValidationError, match="requires LLM_PROVIDER=bedrock"):
        s(app_env=env, llm_model="claude-haiku-5-5", anthropic_api_key="k", **PROD)


@pytest.mark.parametrize("provider", ["nvidia", "ollama", "openai"])
def test_only_anthropic_and_bedrock_exist(provider: str) -> None:
    with pytest.raises(ValidationError):
        s(llm_provider=provider)


def test_anthropic_still_requires_its_key() -> None:
    settings = s(llm_model="claude-test")
    assert not settings.llm_configured
    with pytest.raises(ConfigurationError):
        build_chat_model(settings)


def bedrock(**kw: object) -> Settings:
    base: dict[str, object] = {
        "llm_provider": "bedrock",
        "llm_model": "anthropic.claude-test-v1:0",
        "bedrock_region": "us-east-1",
    }
    return s(**(base | kw))


def test_bedrock_builds_a_converse_model_from_the_aws_credential_chain() -> None:
    settings = bedrock(
        llm_max_output_tokens=1024, llm_request_timeout_seconds=90, llm_max_retries=2
    )
    assert settings.llm_configured and settings.anthropic_api_key is None
    model = build_chat_model(settings)
    assert model.__class__.__name__ == "ChatBedrockConverse"
    assert model.model_id == "anthropic.claude-test-v1:0"  # type: ignore[attr-defined]
    assert model.region_name == "us-east-1"  # type: ignore[attr-defined]
    assert model.max_tokens == 1024  # type: ignore[attr-defined]
    assert model.config.read_timeout == 90  # type: ignore[attr-defined]
    assert model.config.retries["total_max_attempts"] == 3  # type: ignore[attr-defined]
    assert model.credentials_profile_name is None  # type: ignore[attr-defined]


def test_bedrock_passes_an_optional_named_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "aws_config"
    config.write_text("[profile beta-worker]\nregion = us-east-1\n")
    creds = tmp_path / "aws_credentials"
    creds.write_text("[beta-worker]\naws_access_key_id = AKIATEST\naws_secret_access_key = test\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    model = build_chat_model(bedrock(bedrock_credentials_profile="beta-worker"))
    assert model.credentials_profile_name == "beta-worker"  # type: ignore[attr-defined]


@pytest.mark.parametrize("env", ["staging", "production"])
def test_bedrock_is_allowed_in_beta_and_production(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL")  # real AWS only outside development/test
    settings = bedrock(
        app_env=env,
        jira_webhook_secret="s" * 32,
        jira_project_keys="SHOP",
    )
    assert settings.llm_configured


def test_bedrock_needs_a_model_and_a_region() -> None:
    for settings in (bedrock(llm_model=None), bedrock(bedrock_region=None)):
        assert not settings.llm_configured
        with pytest.raises(ConfigurationError):
            build_chat_model(settings)


@pytest.mark.parametrize("region", ["US-EAST-1", "us east 1", "https://bedrock.example.com"])
def test_bedrock_region_is_a_plain_region_name(region: str) -> None:
    with pytest.raises(ValidationError):
        bedrock(bedrock_region=region)


@pytest.mark.parametrize(
    "kw",
    [
        {"llm_model": "claude-test", "anthropic_api_key": "k"},
        {"llm_provider": "bedrock", "llm_model": "m", "bedrock_region": "us-east-1"},
    ],
)
def test_every_model_carries_its_context_window_for_summarisation(kw: dict[str, object]) -> None:
    model = build_chat_model(s(**kw, llm_context_tokens=200_000))
    assert model.profile is not None and model.profile["max_input_tokens"] == 200_000


@pytest.mark.parametrize(
    "kw",
    [
        {"llm_model": "claude-test", "anthropic_api_key": "k"},
        {"llm_provider": "bedrock", "llm_model": "m", "bedrock_region": "us-east-1"},
    ],
)
def test_the_production_chat_models_never_stream(kw: dict[str, object]) -> None:
    """The cost handlers ignore on_llm_error's response because a non-streaming call that fails
    has no partial usage. Enabling streaming would break that (usage streamed before an error
    would go unrecorded and uncharged), so this trips first: see docs/LLM-COST-TRACKING.md."""
    model = build_chat_model(s(**kw))
    assert "streaming" not in model.model_fields_set
    assert model._should_stream(async_api=True, run_manager=None) is False
    assert model._should_stream(async_api=False, run_manager=None) is False


def test_model_calls_can_be_paced_client_side() -> None:
    paced = build_chat_model(s(llm_model="m", anthropic_api_key="k", llm_requests_per_minute=30))
    assert paced.rate_limiter is not None
    assert paced.rate_limiter.requests_per_second == 0.5  # type: ignore[attr-defined]
    assert build_chat_model(s(llm_model="m", anthropic_api_key="k")).rate_limiter is None


# ---- provider failures over a fake transport (no network) -----------------------------------


async def test_anthropic_retries_rate_limits_within_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One representative case; 401/403 (never retried) are asserted with their error codes and
    ledger rows in tests/unit/test_coding_agent.py::
    test_provider_http_failures_end_the_run_with_a_fixed_non_secret_code."""
    import httpx2
    from langchain_core.messages import HumanMessage

    from tests.support.models import anthropic_over_fake_http

    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "refused"}}
    received = anthropic_over_fake_http(monkeypatch, lambda _: httpx2.Response(429, json=body))
    model = build_chat_model(
        s(llm_model="claude-haiku-5-5", anthropic_api_key="k", llm_max_retries=2)
    )
    with pytest.raises(Exception) as err:
        await model.ainvoke([HumanMessage(content="hi")])
    assert getattr(err.value, "status_code", None) == 429
    assert len(received) == 3  # the first request plus LLM_MAX_RETRIES retries, then it stops


@pytest.mark.parametrize(
    ("status", "error_type", "expected_requests"),
    [(429, "ThrottlingException", 3), (403, "AccessDeniedException", 1)],
)
def test_bedrock_retries_throttling_within_the_policy_and_never_retries_access_denied(
    monkeypatch: pytest.MonkeyPatch, status: int, error_type: str, expected_requests: int
) -> None:
    import botocore.retries.standard  # type: ignore[import-untyped]
    from botocore.awsrequest import AWSResponse  # type: ignore[import-untyped]
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]
    from langchain_core.messages import HumanMessage

    for key, value in {
        "AWS_ACCESS_KEY_ID": "AKIATEST",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_EC2_METADATA_DISABLED": "true",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setattr(  # retries do not sleep in tests
        botocore.retries.standard.ExponentialBackoff, "delay_amount", lambda *_: 0
    )

    class Raw:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def stream(self, **_: object) -> Iterator[bytes]:
            yield self.data

    received: list[object] = []

    def send(request: object, **_: object) -> AWSResponse:
        received.append(request)
        headers = {"x-amzn-ErrorType": error_type, "content-type": "application/json"}
        return AWSResponse(request.url, status, headers, Raw(b'{"message":"refused"}'))  # type: ignore[attr-defined]

    model = build_chat_model(bedrock(llm_max_retries=2))
    model.client.meta.events.register("before-send.bedrock-runtime.Converse", send)  # type: ignore[attr-defined]
    with pytest.raises(ClientError) as err:
        model.invoke([HumanMessage(content="hi")])
    assert err.value.response["Error"]["Code"] == error_type
    assert len(received) == expected_requests


# ---- bedrock via the local Floci proxy (ACCA_BEDROCK_ENDPOINT_URL) ----------------------------

FLOCI = "http://127.0.0.1:4577"


def floci_bedrock(**kw: object) -> Settings:
    return bedrock(**({"ACCA_BEDROCK_ENDPOINT_URL": FLOCI + "/"} | kw))


def test_floci_mode_uses_the_local_endpoint_and_the_dummy_credentials_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The developer's AWS identity and endpoint settings are never read: a missing profile, a
    bogus endpoint in the environment and in the config file change nothing."""
    from botocore.awsrequest import AWSResponse
    from langchain_core.messages import HumanMessage

    config = tmp_path / "aws_config"
    config.write_text("[default]\nendpoint_url = http://203.0.113.1:1\nregion = ap-south-1\n")
    for key, value in {
        "AWS_PROFILE": "does-not-exist",
        "AWS_ENDPOINT_URL": "http://203.0.113.1:1",
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": "http://203.0.113.1:1",
        "AWS_CONFIG_FILE": str(config),
        "AWS_ACCESS_KEY_ID": "AKIAREALLOOKING",
        "AWS_SECRET_ACCESS_KEY": "real-looking",
        "AWS_SESSION_TOKEN": "real-looking-token",
    }.items():
        monkeypatch.setenv(key, value)
    settings = floci_bedrock()
    assert (
        settings.bedrock_endpoint_url == FLOCI and settings.llm_ledger_provider == "bedrock-floci"
    )
    model = build_chat_model(settings)
    assert model.__class__.__name__ == "ChatBedrockConverse"
    for client in (model.client, model.bedrock_client):  # type: ignore[attr-defined]
        assert client.meta.endpoint_url == FLOCI
        creds = client._request_signer._credentials
        assert (creds.access_key, creds.secret_key, creds.token) == ("test", "test", None)
    assert model.config.ignore_configured_endpoint_urls is True  # type: ignore[attr-defined]
    assert model.client.meta.config.retries["total_max_attempts"] == 3  # type: ignore[attr-defined]

    class Raw:
        def stream(self, **_: object) -> Iterator[bytes]:
            yield b'{"message":"stop here"}'

    sent: list[object] = []

    def send(request: object, **_: object) -> AWSResponse:
        sent.append(request)
        headers = {"x-amzn-ErrorType": "ValidationException", "content-type": "application/json"}
        return AWSResponse(request.url, 400, headers, Raw())  # type: ignore[attr-defined]

    model.client.meta.events.register("before-send.bedrock-runtime.Converse", send)  # type: ignore[attr-defined]
    with pytest.raises(Exception, match="stop here"):
        model.invoke([HumanMessage(content="hi")])
    url = sent[0].url  # type: ignore[attr-defined]
    assert url.startswith(FLOCI + "/model/") and "Credential=test/" in str(
        sent[0].headers["Authorization"]  # type: ignore[attr-defined]
    )


def test_real_bedrock_has_no_endpoint_override_and_keeps_its_label() -> None:
    settings = bedrock()
    assert settings.bedrock_endpoint_url is None and settings.llm_ledger_provider == "bedrock"
    model = build_chat_model(settings)
    assert model.endpoint_url is None  # type: ignore[attr-defined]
    assert s(llm_model="m", anthropic_api_key="k").llm_ledger_provider == "anthropic"


def test_floci_mode_never_streams_on_its_own() -> None:
    model = build_chat_model(floci_bedrock())
    assert "streaming" not in model.model_fields_set
    assert model._should_stream(async_api=True, run_manager=None) is False


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_environments_refuse_the_bedrock_endpoint(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACCA_AWS_ENDPOINT_URL")
    with pytest.raises(ConfigurationError, match="must not set ACCA_BEDROCK_ENDPOINT_URL"):
        floci_bedrock(app_env=env, **PROD)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:4566",
        "http://bedrock-runtime.us-east-1.amazonaws.com",
        "http://bedrock-runtime.us-east-1.api.aws",
        "http://bedrock-runtime.cn-north-1.amazonaws.com.cn",
        "http://floci.example.com:4566",
        "http://203.0.113.1:4566",
        "http://user:pw@127.0.0.1:4566",
        "http://127.0.0.1:4566/v1",
        "http://127.0.0.1:notaport",
    ],
)
def test_non_local_bedrock_endpoints_are_refused(url: str) -> None:
    with pytest.raises(ConfigurationError, match="ACCA_BEDROCK_ENDPOINT_URL must be http://"):
        floci_bedrock(ACCA_BEDROCK_ENDPOINT_URL=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:4566",
        "http://localhost:4566",
        "http://[::1]:4566",
        "http://acca-floci:4566",
    ],
)
def test_local_bedrock_endpoints_are_accepted(url: str) -> None:
    assert floci_bedrock(ACCA_BEDROCK_ENDPOINT_URL=url).bedrock_endpoint_url == url


def test_the_bedrock_endpoint_needs_bedrock_and_no_profile() -> None:
    with pytest.raises(ConfigurationError, match="needs LLM_PROVIDER=bedrock"):
        s(llm_model="m", anthropic_api_key="k", ACCA_BEDROCK_ENDPOINT_URL=FLOCI)
    with pytest.raises(ConfigurationError, match="unset BEDROCK_CREDENTIALS_PROFILE"):
        floci_bedrock(bedrock_credentials_profile="beta-worker")


def test_an_empty_bedrock_endpoint_means_real_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACCA_BEDROCK_ENDPOINT_URL", "")
    assert bedrock().bedrock_endpoint_url is None
    monkeypatch.setenv("ACCA_BEDROCK_ENDPOINT_URL", FLOCI)
    assert bedrock().bedrock_endpoint_url == FLOCI


def test_the_cost_ledger_and_reports_label_floci_calls_bedrock_floci() -> None:
    import uuid

    from ai_code_change_agent.composition import build_cost_reporter

    reporter = build_cost_reporter(object(), floci_bedrock(), object(), uuid.uuid4())  # type: ignore[arg-type]
    assert reporter._provider == "bedrock-floci"
