"""Benchmark-only OpenAI JSON client."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from benchmarks.toolbox import parse_json_object


@dataclass(frozen=True)
class OpenAIUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None


@dataclass(frozen=True)
class OpenAIJsonResult:
    value: dict[str, Any]
    text: str
    usage: OpenAIUsage


class OpenAIJsonClient:
    """Calls OpenAI and asks for one JSON object.

    Pricing is intentionally approximate and configurable by model map. The
    benchmark treats cost as directional evidence, not billing truth.
    """

    PRICE_PER_1M_TOKENS: dict[str, tuple[float, float]] = {
        "gpt-5.4-mini": (0.25, 2.0),
        "gpt-5-mini": (0.25, 2.0),
        "gpt-4.1-mini": (0.40, 1.60),
        "gpt-4o-mini": (0.15, 0.60),
    }

    def __init__(self, *, model: str, api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for live benchmark runs")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install benchmark dependencies: pip install -r benchmarks/requirements.txt") from exc
        self.client = OpenAI(api_key=self.api_key)

    def complete_json(self, system: str, user: str) -> OpenAIJsonResult:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        text = response.choices[0].message.content or "{}"
        value = parse_json_object(text)
        if value is None:
            value = {"error": "model_returned_non_json", "raw": text}
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        return OpenAIJsonResult(
            value=value,
            text=text,
            usage=OpenAIUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=self.estimate_cost(input_tokens, output_tokens),
            ),
        )

    def estimate_cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        if input_tokens is None and output_tokens is None:
            return None
        input_price, output_price = self.PRICE_PER_1M_TOKENS.get(self.model, (0.0, 0.0))
        return round(
            ((input_tokens or 0) * input_price + (output_tokens or 0) * output_price) / 1_000_000,
            8,
        )


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
