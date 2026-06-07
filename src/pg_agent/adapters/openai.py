"""OpenAI Responses API adapter for Rowplane workers.

The adapter is intentionally thin: it turns worker prompt messages into a model
request, returns the model's text command, and exposes usage metadata for the
database-owned model-call ledger.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class OpenAIModelClient:
    """Worker-compatible model client backed by the OpenAI Responses API.

    Parameters that affect real API calls are kept explicit and optional. Extra
    Responses API fields can be supplied through ``request_options`` so this
    adapter does not need to chase every SDK parameter.
    """

    def __init__(
        self,
        *,
        model: str = "gpt-5",
        api_key: str | None = None,
        client: Any | None = None,
        instructions: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        request_options: Mapping[str, Any] | None = None,
        estimated_call_cost_usd: float = 0.0,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ) -> None:
        self.model = model
        self.instructions = instructions
        self.max_output_tokens = max_output_tokens
        self.temperature = temperature
        self.request_options = dict(request_options or {})
        self.estimated_call_cost_usd = float(estimated_call_cost_usd)
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.last_usage: dict[str, Any] = {}
        self.last_response: Any | None = None

        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - exercised without SDK.
                raise RuntimeError(
                    "OpenAIModelClient requires the optional OpenAI SDK. "
                    "Install it with: pip install -e '.[openai]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            client = OpenAI(**kwargs)
        self.client = client

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        request: dict[str, Any] = {
            "model": self.model,
            "input": _to_responses_input(messages),
        }
        if self.instructions is not None:
            request["instructions"] = self.instructions
        if self.max_output_tokens is not None:
            request["max_output_tokens"] = self.max_output_tokens
        if self.temperature is not None:
            request["temperature"] = self.temperature
        request.update(self.request_options)

        response = self.client.responses.create(**request)
        self.last_response = response
        self.last_usage = _extract_usage(
            response,
            input_cost_per_million=self.input_cost_per_million,
            output_cost_per_million=self.output_cost_per_million,
        )
        text = _extract_output_text(response)
        if text is None:
            raise RuntimeError(_missing_output_text_error(response))
        return text

    def get_last_usage(self) -> Mapping[str, Any]:
        return self.last_usage


def _to_responses_input(messages: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        normalized.append({"role": role, "content": str(content)})
    return normalized


def _extract_output_text(response: Any) -> str | None:
    output_text = _get(response, "output_text")
    if isinstance(output_text, str) and output_text:
        return output_text

    parts: list[str] = []
    for item in _as_list(_get(response, "output")):
        for content in _as_list(_get(item, "content")):
            text = _get(content, "text")
            if isinstance(text, str):
                parts.append(text)
    if parts:
        return "".join(parts)
    return None


def _missing_output_text_error(response: Any) -> str:
    status = _get(response, "status")
    incomplete_details = _get(response, "incomplete_details")
    if status == "incomplete" and incomplete_details is not None:
        reason = _get(incomplete_details, "reason") or incomplete_details
        return f"OpenAI response did not contain output text; response was incomplete: {reason}"
    if status is not None:
        return f"OpenAI response did not contain output text; response status was {status}"
    return "OpenAI response did not contain output text"


def _extract_usage(
    response: Any,
    *,
    input_cost_per_million: float | None,
    output_cost_per_million: float | None,
) -> dict[str, Any]:
    usage = _get(response, "usage")
    input_tokens = _int_or_none(
        _first_present(usage, "input_tokens", "prompt_tokens")
    )
    output_tokens = _int_or_none(
        _first_present(usage, "output_tokens", "completion_tokens")
    )
    total_tokens = _int_or_none(_get(usage, "total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    result: dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    estimated_cost = _estimate_cost(
        input_tokens,
        output_tokens,
        input_cost_per_million=input_cost_per_million,
        output_cost_per_million=output_cost_per_million,
    )
    if estimated_cost is not None:
        result["estimated_cost_usd"] = estimated_cost
    return result


def _estimate_cost(
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    input_cost_per_million: float | None,
    output_cost_per_million: float | None,
) -> float | None:
    if (
        input_tokens is None
        or output_tokens is None
        or input_cost_per_million is None
        or output_cost_per_million is None
    ):
        return None
    return (
        (input_tokens / 1_000_000) * float(input_cost_per_million)
        + (output_tokens / 1_000_000) * float(output_cost_per_million)
    )


def _get(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _first_present(value: Any, *keys: str) -> Any:
    for key in keys:
        item = _get(value, key)
        if item is not None:
            return item
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
