"""Chat model construction. The Deep Agent receives a LangChain chat model from here; provider
SDK specifics stay in this module.

Two providers, three modes:
- `anthropic`: the Claude API, for local development and tests (Settings refuses it in staging and
  production);
- `bedrock` without an endpoint override: AWS Bedrock Converse for staging and production;
  credentials come from the worker's standard AWS chain (IAM role, profile, environment), never
  from settings;
- `bedrock` with ACCA_BEDROCK_ENDPOINT_URL (development/test only, loopback or a single-label
  Docker host, enforced by Settings): the Bedrock Runtime of the local Floci emulator, whose
  `proxy` backend forwards Converse/ConverseStream to an OpenAI-compatible endpoint. The clients
  use the dummy credentials test/test from a session without profile or AWS files, and ignore
  AWS_ENDPOINT_URL*, so neither the developer's AWS identity nor real Bedrock is ever reached."""

from typing import Any

from botocore.config import Config  # type: ignore[import-untyped]
from langchain_anthropic import ChatAnthropic
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter
from pydantic import SecretStr

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError
from ai_code_change_agent.integrations.aws.clients import (
    LOCAL_ACCESS_KEY_ID,
    LOCAL_SECRET_ACCESS_KEY,
    local_session,
)


def build_chat_model(settings: Settings) -> BaseChatModel:
    """No network call happens here; a missing key or model fails before any run starts. The model
    profile carries the context window so Deep Agents' summarisation triggers before overflow."""
    model = _build(settings)
    model.profile = {**(model.profile or {}), "max_input_tokens": settings.llm_context_tokens}
    if settings.llm_requests_per_minute:
        # Pace calls before the provider rate-limits (backoff remains the second line).
        model.rate_limiter = InMemoryRateLimiter(
            requests_per_second=settings.llm_requests_per_minute / 60, max_bucket_size=1
        )
    return model


def _build(settings: Settings) -> BaseChatModel:
    if settings.llm_provider == "bedrock":
        if settings.llm_model is None or settings.bedrock_region is None:
            msg = "LLM_MODEL and BEDROCK_REGION are required for the coding agent"
            raise ConfigurationError(msg)
        config = Config(
            connect_timeout=10,
            read_timeout=settings.llm_request_timeout_seconds,
            retries={"total_max_attempts": settings.llm_max_retries + 1, "mode": "standard"},
        )
        if settings.bedrock_endpoint_url is not None:
            return _floci_bedrock(settings, settings.llm_model, settings.bedrock_region, config)
        return ChatBedrockConverse(
            model_id=settings.llm_model,
            region_name=settings.bedrock_region,
            credentials_profile_name=settings.bedrock_credentials_profile,
            max_tokens=settings.llm_max_output_tokens,
            config=config,
        )
    if settings.llm_model is None or settings.anthropic_api_key is None:
        msg = "LLM_MODEL and ANTHROPIC_API_KEY are required for the coding agent"
        raise ConfigurationError(msg)
    return ChatAnthropic(
        model=settings.llm_model,
        api_key=settings.anthropic_api_key,
        timeout=settings.llm_request_timeout_seconds,
        max_retries=settings.llm_max_retries,
        max_tokens=settings.llm_max_output_tokens,
    )


def _floci_bedrock(settings: Settings, model_id: str, region: str, base: Config) -> BaseChatModel:
    """Both clients (runtime and control plane) are built here and handed in, so langchain_aws
    never builds its own from the environment (AWS_PROFILE, AWS_* keys, a Bedrock API key)."""
    endpoint = settings.bedrock_endpoint_url
    config = base.merge(Config(ignore_configured_endpoint_urls=True))
    session = local_session(region)

    def client(service: str) -> Any:
        return session.client(service, region_name=region, endpoint_url=endpoint, config=config)

    return ChatBedrockConverse(
        model_id=model_id,
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=SecretStr(LOCAL_ACCESS_KEY_ID),
        aws_secret_access_key=SecretStr(LOCAL_SECRET_ACCESS_KEY),
        aws_session_token=None,
        max_tokens=settings.llm_max_output_tokens,
        config=config,
        client=client("bedrock-runtime"),
        bedrock_client=client("bedrock"),
    )
