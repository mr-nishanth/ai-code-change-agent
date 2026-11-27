"""A scripted, tool-calling chat model for agent tests (no provider, no network), and a fake
HTTP layer for the real provider clients."""

from collections.abc import Callable, Sequence
from typing import Any

import anthropic
import httpx2
import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    """Returns the scripted AIMessages in order; records every prompt it was given."""

    script: list[AIMessage]
    seen: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        self.seen.append([])  # marks a bind; keeps the same script
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        if not self.script:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="done"))])
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])


def call(name: str, args: dict[str, Any], i: int) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{i}"}])


def anthropic_over_fake_http(
    monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx2.Request], httpx2.Response]
) -> list[httpx2.Request]:
    """Routes every ChatAnthropic request through `respond` instead of the network. The real
    Anthropic SDK client (and its retry policy) stays in place; only its backoff wait is zero, so
    retries do not sleep. Returns the list of requests the provider "received"."""
    import langchain_anthropic.chat_models as chat_models

    received: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        received.append(request)
        return respond(request)

    def client(**_: Any) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))

    monkeypatch.setattr(chat_models, "_get_default_async_httpx_client", client)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "_calculate_retry_timeout", lambda *_a, **_k: 0.0)
    return received
