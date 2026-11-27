"""Bedrock through the Floci Bedrock Runtime `proxy` backend, end to end, with no real model.

A throwaway Floci 2.5.0 (the pinned digest of infra/docker-compose.yml, no Docker socket, console
disabled, in-memory storage) shares the network namespace of a sidecar that runs the fake
OpenAI-compatible upstream (tests/support/fake_openai_upstream.py) on 127.0.0.1, so the proxy URL
is loopback and no request can leave the machine. Floci is published on 127.0.0.1:4577 only; both
containers are removed afterwards. The application's own factory (ACCA_BEDROCK_ENDPOINT_URL)
builds the ChatBedrockConverse model.

Floci variables, all from the 2.5.0 docs: docs/services/bedrock-runtime.md:34-41
(FLOCI_SERVICES_BEDROCK_RUNTIME_BACKEND, _PROXY_URL, _PROXY_API_KEY, _PROXY_MODEL_MAPPING) and the
environment-variable reference (FLOCI_DEFAULT_REGION, FLOCI_SERVICES_UI_ENABLED)."""

import json
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool

from ai_code_change_agent.application.costs.usage import LlmCallRecord, UsageTracker
from ai_code_change_agent.composition import build_price_table
from ai_code_change_agent.core.config import Settings
from ai_code_change_agent.providers.llm.factory import build_chat_model
from tests.support.floci import LOOPBACK_HOSTS, wait_until

pytestmark = [pytest.mark.floci, pytest.mark.docker, pytest.mark.integration]

FLOCI_IMAGE = (
    "floci/floci:2.5.0@sha256:a0fbf944d7f338cc0856f1ef83831ea749ff2be71956eb7a7fce08fc21458e19"
)
FAKE_IMAGE = (
    "python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b"
)
ENDPOINT = "http://127.0.0.1:4577"
FAKE_PORT = 8099
PROXY_URL = f"http://127.0.0.1:{FAKE_PORT}/v1"  # inside the shared network namespace
DUMMY_KEY = "dummy-not-a-secret"  # the fake upstream accepts anything
BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
UPSTREAM_MODEL = "claude-haiku-4-5"
FAKE_SCRIPT = Path(__file__).resolve().parents[2] / "support" / "fake_openai_upstream.py"
SIDECAR_TMP = "/tmp"  # noqa: S108 - the sidecar's own tmpfs, not the host's
LOG = f"{SIDECAR_TMP}/requests.jsonl"


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed docker argv in a test helper
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _image(ref: str) -> None:
    if _docker("image", "inspect", ref, timeout=30).returncode == 0:
        return
    pulled = _docker("pull", ref, timeout=600)
    if pulled.returncode != 0:
        pytest.skip(f"cannot pull {ref}: {pulled.stderr[-300:]}")


@dataclass
class Proxy:
    endpoint: str
    floci_env: dict[str, str]
    fake: str

    def upstream_requests(self) -> list[dict[str, Any]]:
        out = _docker("exec", self.fake, "cat", LOG, timeout=30)
        assert out.returncode == 0, out.stderr
        return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def proxy(docker_ready: None) -> Iterator[Proxy]:
    sandboxes = _docker("ps", "-q", "--filter", "label=aica.role=agent-sandbox", timeout=30)
    if sandboxes.stdout.strip():
        pytest.skip("a live agent sandbox is running; Docker tests wait for it to finish")
    _image(FLOCI_IMAGE)
    _image(FAKE_IMAGE)
    suffix = uuid.uuid4().hex[:12]
    fake, floci = f"s2116-fake-upstream-{suffix}", f"s2116-floci-bedrock-{suffix}"
    env = {
        "FLOCI_DEFAULT_REGION": "us-east-1",
        "FLOCI_SERVICES_UI_ENABLED": "false",
        "FLOCI_SERVICES_BEDROCK_RUNTIME_BACKEND": "proxy",
        "FLOCI_SERVICES_BEDROCK_RUNTIME_PROXY_URL": PROXY_URL,
        "FLOCI_SERVICES_BEDROCK_RUNTIME_PROXY_API_KEY": DUMMY_KEY,
        "FLOCI_SERVICES_BEDROCK_RUNTIME_PROXY_MODEL_MAPPING": f"{BEDROCK_MODEL}={UPSTREAM_MODEL}",
    }
    try:
        started = _docker(
            "run", "-d", "--name", fake, "--label", "acca.test=s2116-bedrock-proxy",
            "-p", "127.0.0.1:4577:4566",  # Floci's port, published by the namespace owner
            "--read-only", "--tmpfs", SIDECAR_TMP, "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "-v", f"{FAKE_SCRIPT}:/fake/upstream.py:ro",
            FAKE_IMAGE, "python", "-I", "/fake/upstream.py", str(FAKE_PORT), LOG,
        )  # fmt: skip
        assert started.returncode == 0, started.stderr
        env_args = [arg for name, value in env.items() for arg in ("-e", f"{name}={value}")]
        started = _docker(
            "run", "-d", "--name", floci, "--label", "acca.test=s2116-bedrock-proxy",
            "--network", f"container:{fake}", *env_args, FLOCI_IMAGE,
        )  # fmt: skip
        assert started.returncode == 0, started.stderr

        def healthy() -> bool:
            try:
                return httpx.get(f"{ENDPOINT}/_floci/health", timeout=2).status_code == 200
            except httpx.HTTPError:
                return False

        wait_until(healthy, timeout=90, interval=1)
        yield Proxy(ENDPOINT, env, fake)
    finally:
        _docker("rm", "-f", "-v", floci, fake, timeout=60)


@pytest.fixture
def settings(proxy: Proxy, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Isolation: the developer's AWS identity and endpoint settings must not matter.
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://203.0.113.1:1")
    return Settings(
        _env_file=None,
        app_env="test",
        llm_provider="bedrock",
        llm_model=BEDROCK_MODEL,
        bedrock_region="us-east-1",
        llm_max_output_tokens=256,
        ACCA_BEDROCK_ENDPOINT_URL=proxy.endpoint,
    )


@tool
def read_file(path: str) -> str:
    """Read a file of the workspace."""
    return f"contents of {path}"


class MemoryStore:
    def __init__(self) -> None:
        self.records: list[LlmCallRecord] = []

    async def add(self, record: LlmCallRecord) -> bool:
        self.records.append(record)
        return True


def test_the_proxy_url_is_loopback_so_nothing_leaves_the_machine(proxy: Proxy) -> None:
    for url in (proxy.endpoint, proxy.floci_env["FLOCI_SERVICES_BEDROCK_RUNTIME_PROXY_URL"]):
        assert urlsplit(url).scheme == "http" and urlsplit(url).hostname in LOOPBACK_HOSTS


async def test_tool_call_and_tool_result_round_trip_are_recorded_as_bedrock_floci(
    proxy: Proxy, settings: Settings
) -> None:
    model = build_chat_model(settings)
    assert model.__class__.__name__ == "ChatBedrockConverse"
    store = MemoryStore()
    tracker = UsageTracker(
        store,
        build_price_table(settings),
        provider=settings.llm_ledger_provider,
        model=BEDROCK_MODEL,
    )
    attempt = uuid.uuid4()
    config: Any = {"callbacks": tracker.handlers(f"attempt:{attempt}:planning:run-1")}
    bound = model.bind_tools([read_file])
    first = await bound.ainvoke([HumanMessage("read the README")], config=config)
    assert isinstance(first, AIMessage)
    assert [(c["name"], c["args"]) for c in first.tool_calls] == [
        ("read_file", {"path": "README.md"})
    ]
    call_id = first.tool_calls[0]["id"]
    assert call_id is not None
    second = await bound.ainvoke(
        [HumanMessage("read the README"), first, ToolMessage("file body", tool_call_id=call_id)],
        config=config,
    )
    assert second.text == "fake reply" and not second.tool_calls

    assert len(store.records) == 2
    for record in store.records:
        assert (record.provider, record.stage, record.attempt_id) == (
            "bedrock-floci",
            "planning",
            attempt,
        )
        assert record.succeeded and record.usage is not None
        assert (record.usage.input_tokens, record.usage.output_tokens) == (11, 7)
        assert record.usage.cache_read_tokens == 0
        assert record.usage.cache_write_5m_tokens == record.usage.cache_write_1h_tokens == 0

    sent = proxy.upstream_requests()
    assert {r["path"] for r in sent} == {"/v1/chat/completions"}
    assert {r["auth"] for r in sent} == {f"Bearer {DUMMY_KEY}"}
    assert {r["body"]["model"] for r in sent} == {UPSTREAM_MODEL}  # the documented mapping
    tool_turn = sent[-1]["body"]["messages"]
    assert tool_turn[-1] == {"role": "tool", "tool_call_id": call_id, "content": "file body"}
    assert sent[-1]["body"]["tools"][0]["function"]["name"] == "read_file"


async def test_a_stream_is_translated_into_bedrock_events(proxy: Proxy, settings: Settings) -> None:
    model = build_chat_model(settings)
    chunks = [c async for c in model.astream([HumanMessage("hi")])]
    assert len(chunks) > 1  # incremental deltas, not one buffered reply
    total = chunks[0]
    for chunk in chunks[1:]:
        total = total + chunk
    assert isinstance(total, AIMessageChunk)
    assert total.text == "fake reply"
    assert total.usage_metadata is not None
    assert (total.usage_metadata["input_tokens"], total.usage_metadata["output_tokens"]) == (11, 7)
    streamed = [r for r in proxy.upstream_requests() if r["body"].get("stream")]
    assert streamed and streamed[-1]["auth"] == f"Bearer {DUMMY_KEY}"
