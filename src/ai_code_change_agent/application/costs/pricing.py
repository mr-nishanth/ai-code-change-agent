"""LLM API pricing: one table, one calculation.

Rates are USD per million tokens from the official Anthropic price list
(https://platform.claude.com/docs/en/about-claude/pricing), checked on PRICING_VERSION's date.
Edit DEFAULT_PRICING here, or point LLM_PRICING_FILE at a JSON file with the same shape, when
prices change; nothing else in the application knows a rate.

The cost of one call is computed from the token categories the provider reported, never from
character counts or request counts:

    cost = input x input_rate + output x output_rate
         + cache_write_5m x 5m_write_rate + cache_write_1h x 1h_write_rate
         + cache_read x cache_read_rate

Anthropic reports uncached input, cache writes and cache reads as separate, non-overlapping counts,
so nothing is counted twice. Claude Haiku 5.5 is priced by prompt length: a request whose prompt
(input + cache writes + cache reads) is over 100,000 tokens pays the long-context rates for all of
its tokens. US-only inference (`inference_geo: "us"`) multiplies every category by 1.1.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

PRICING_VERSION = "anthropic-api-2026-11-25"
PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
_MTOK = Decimal(1_000_000)
US_GEO_MULTIPLIER = Decimal("1.1")
# Anthropic web search: $10 per 1,000 searches, on top of the tokens (same price list).
WEB_SEARCH_USD_PER_REQUEST = Decimal("0.01")


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal


@dataclass(frozen=True)
class ModelPricing:
    standard: Rates
    long_context: Rates | None = None
    long_context_threshold: int | None = None  # prompt tokens; above it, long_context applies

    def rates_for(self, prompt_tokens: int) -> tuple[Rates, bool]:
        if (
            self.long_context is not None
            and self.long_context_threshold is not None
            and prompt_tokens > self.long_context_threshold
        ):
            return self.long_context, True
        return self.standard, False


@dataclass(frozen=True)
class TokenUsage:
    """Token counts exactly as the provider reported them (non-overlapping categories)."""

    input_tokens: int
    output_tokens: int
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def cache_write_tokens(self) -> int:
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


@dataclass(frozen=True)
class CallCost:
    input: Decimal
    output: Decimal
    cache_write: Decimal
    cache_read: Decimal
    long_context: bool

    @property
    def total(self) -> Decimal:
        return self.input + self.output + self.cache_write + self.cache_read


def _rates(i: str, o: str, w5: str, w1: str, r: str) -> Rates:
    return Rates(Decimal(i), Decimal(o), Decimal(w5), Decimal(w1), Decimal(r))


DEFAULT_PRICING: Mapping[str, ModelPricing] = {
    "claude-haiku-5-5": ModelPricing(
        standard=_rates("0.10", "0.50", "0.125", "0.20", "0.01"),
        long_context=_rates("0.50", "2.50", "0.625", "1.00", "0.05"),
        long_context_threshold=100_000,
    ),
    "claude-sonnet-5-5": ModelPricing(standard=_rates("2.00", "10.00", "2.50", "4.00", "0.10")),
    # Opus 5.5: $4 / $20; cache writes 1.25x (5m) / 2x (1h); cache hits 0.05x ($0.20).
    "claude-opus-5-5": ModelPricing(standard=_rates("4.00", "20.00", "5.00", "8.00", "0.20")),
}


def bedrock_base_model(model_id: str) -> str:
    """us.anthropic.claude-sonnet-5-5-v1:0 -> claude-sonnet-5-5 (inference profile and
    version suffix removed). Non-Bedrock ids are returned unchanged."""
    base = re.sub(r"^(?:[a-z]{2,8}\.)?anthropic\.", "", model_id.strip())
    return re.sub(r"(?:-\d{8})?(?:-v\d+(?::\d+)?)?$", "", base)


class PriceTable:
    def __init__(
        self, models: Mapping[str, ModelPricing] | None = None, version: str = PRICING_VERSION
    ) -> None:
        self._models = dict(DEFAULT_PRICING if models is None else models)
        self.version = version

    @classmethod
    def from_file(cls, path: Path) -> "PriceTable":
        """{"version": "...", "models": {"<id>": {"input": "0.10", "output": "0.50",
        "cache_write_5m": "...", "cache_write_1h": "...", "cache_read": "...",
        "long_context": {same keys}, "long_context_threshold": 100000}}}"""
        doc: Any = json.loads(path.read_text(encoding="utf-8"))
        models: dict[str, ModelPricing] = {}
        for model_id, spec in dict(doc["models"]).items():
            long_spec = spec.get("long_context")
            models[str(model_id)] = ModelPricing(
                standard=_from_doc(spec),
                long_context=_from_doc(long_spec) if long_spec else None,
                long_context_threshold=(
                    int(spec["long_context_threshold"])
                    if spec.get("long_context_threshold") is not None
                    else None
                ),
            )
        return cls(models, str(doc.get("version", f"file:{path.name}")))

    def estimate(
        self, model: str, usage: TokenUsage, *, inference_geo: str | None = None
    ) -> CallCost | None:
        """For cost GUARDS only: the configured price, or else the first-party list price of the
        same Claude model behind a Bedrock id (us.anthropic.claude-...-v1:0). Reports keep using
        price(), so an unconfigured Bedrock price is still shown as unknown, never guessed."""
        return self.price(model, usage, inference_geo=inference_geo) or self.price(
            bedrock_base_model(model), usage, inference_geo=inference_geo
        )

    def knows(self, model: str) -> bool:
        return self._lookup(model) is not None

    def _lookup(self, model: str) -> ModelPricing | None:
        if model in self._models:
            return self._models[model]
        for key in sorted(self._models, key=len, reverse=True):  # dated or suffixed ids
            if model.startswith(key + "-") or model.startswith(key + "@"):
                return self._models[key]
        return None

    def price(
        self, model: str, usage: TokenUsage, *, inference_geo: str | None = None
    ) -> CallCost | None:
        """None when the model has no configured price (the cost is then unknown, not zero)."""
        pricing = self._lookup(model)
        if pricing is None:
            return None
        rates, long_context = pricing.rates_for(usage.prompt_tokens)
        factor = US_GEO_MULTIPLIER if (inference_geo or "").lower() == "us" else Decimal(1)

        def cost(tokens: int, rate: Decimal) -> Decimal:
            return Decimal(tokens) * rate * factor / _MTOK

        return CallCost(
            input=cost(usage.input_tokens, rates.input),
            output=cost(usage.output_tokens, rates.output),
            cache_write=cost(usage.cache_write_5m_tokens, rates.cache_write_5m)
            + cost(usage.cache_write_1h_tokens, rates.cache_write_1h),
            cache_read=cost(usage.cache_read_tokens, rates.cache_read),
            long_context=long_context,
        )


def _from_doc(spec: Mapping[str, Any]) -> Rates:
    return Rates(
        Decimal(str(spec["input"])),
        Decimal(str(spec["output"])),
        Decimal(str(spec["cache_write_5m"])),
        Decimal(str(spec["cache_write_1h"])),
        Decimal(str(spec["cache_read"])),
    )
