"""Chat model construction. The Deep Agent receives a LangChain chat model from here; provider
SDK specifics stay in this module.

Two providers:
- `anthropic`: the Claude API, for local development and tests (Settings refuses it in staging and
  production);
- `bedrock`: AWS Bedrock Converse for staging and production; credentials come from the worker's
  standard AWS chain (IAM role, profile, environment), never from settings."""

from botocore.config import Config  # type: ignore[import-untyped]
from langchain_anthropic import ChatAnthropic
from langchain_aws import ChatBedrockConverse
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.rate_limiters import InMemoryRateLimiter

from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.core.errors import ConfigurationError


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
        return ChatBedrockConverse(
            model_id=settings.llm_model,
            region_name=settings.bedrock_region,
            credentials_profile_name=settings.bedrock_credentials_profile,
            max_tokens=settings.llm_max_output_tokens,
            config=Config(
                connect_timeout=10,
                read_timeout=settings.llm_request_timeout_seconds,
                retries={"total_max_attempts": settings.llm_max_retries + 1, "mode": "standard"},
            ),
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
