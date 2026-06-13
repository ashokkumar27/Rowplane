"""Shared helpers for framework-facing Rowplane intent adapters."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pg_agent.runtime.intents import parse_intent

DEFAULT_INTENT_BRIDGE_INSTRUCTIONS = """You are a Rowplane intent planner.
Return exactly one JSON object and no prose.
The object must be a RowplaneIntent with schema_version 1.
Allowed intent values are final_answer, tool_request, clarification_request, memory_proposal, delegation_request, and failure.
Use registered_tools from the prompt state only to choose tool names and arguments.
Do not execute tools. Do not call external APIs. Do not decide whether approval is required.
Do not return tool_calls, function calls, arrays, multiple JSON objects, or framework-native tool actions.
Rowplane will validate policy, approvals, permissions, idempotency, events, and execution."""

MessagesInputBuilder = Callable[[Sequence[Mapping[str, str]]], Any]
OutputExtractor = Callable[[Any], Any]


def messages_to_text(messages: Sequence[Mapping[str, str]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user")).upper()
        content = str(message.get("content", ""))
        parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts)


def messages_to_langchain_messages(messages: Sequence[Mapping[str, str]]) -> list[tuple[str, str]]:
    normalized: list[tuple[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        normalized.append((role, str(message.get("content", ""))))
    return normalized


def coerce_intent_text(output: Any) -> str:
    if isinstance(output, str):
        text = normalize_json_object_text(output)
        if not text:
            raise RuntimeError("framework intent output was empty")
        parse_intent(text)
        return text

    model_dump = getattr(output, "model_dump", None)
    if callable(model_dump):
        output = model_dump()
    elif hasattr(output, "dict") and callable(getattr(output, "dict")):
        output = output.dict()

    if _looks_like_native_tool_call(output):
        raise RuntimeError("framework-native tool calls are not Rowplane intents")

    if isinstance(output, Mapping):
        parse_intent(dict(output))
        return json.dumps(output, sort_keys=True, default=str)

    if isinstance(output, list | tuple):
        raise RuntimeError("framework output must contain exactly one Rowplane intent object")

    raise RuntimeError("framework intent output must be a JSON string, mapping, or Pydantic-like object")


def normalize_json_object_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    unfenced = _strip_json_fence(stripped)
    parsed = _parse_single_json_object(unfenced)
    if parsed is not None:
        return json.dumps(parsed, sort_keys=True, default=str)

    start = unfenced.find("{")
    if start >= 0:
        parsed = _parse_single_json_object(unfenced[start:])
        if parsed is not None:
            return json.dumps(parsed, sort_keys=True, default=str)

    return stripped


def extract_framework_output(result: Any) -> Any:
    for key in ("structured_response", "final_output", "output_text", "content"):
        value = _get(result, key)
        if value is not None:
            return value

    if isinstance(result, Mapping):
        if "tool_calls" in result or "function_call" in result:
            raise RuntimeError("framework-native tool calls are not Rowplane intents")
        messages = result.get("messages")
        if messages is not None:
            message = _last_message(messages)
            if message is not None:
                return _extract_message_content(message)
        if "output" in result:
            return result["output"]

    messages = _get(result, "messages")
    if messages is not None:
        message = _last_message(messages)
        if message is not None:
            return _extract_message_content(message)

    return result


def extract_usage(
    result: Any,
    *,
    input_cost_per_million: float | None,
    output_cost_per_million: float | None,
) -> dict[str, Any]:
    usage = _find_usage(result)
    input_tokens = _int_or_none(_first_present(usage, "input_tokens", "prompt_tokens"))
    output_tokens = _int_or_none(_first_present(usage, "output_tokens", "completion_tokens"))
    total_tokens = _int_or_none(_get(usage, "total_tokens"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    output: dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    estimated = _estimate_cost(
        input_tokens,
        output_tokens,
        input_cost_per_million=input_cost_per_million,
        output_cost_per_million=output_cost_per_million,
    )
    if estimated is not None:
        output["estimated_cost_usd"] = estimated
    return output


def combine_usage(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        result[key] = _sum_optional_int(left.get(key), right.get(key))
    estimated = _sum_optional_float(left.get("estimated_cost_usd"), right.get("estimated_cost_usd"))
    if estimated is not None:
        result["estimated_cost_usd"] = estimated
    return result


def import_optional_symbol(package_name: str, symbol_name: str, extra_name: str, client_name: str) -> Any:
    try:
        module = __import__(package_name, fromlist=[symbol_name])
    except ImportError as exc:  # pragma: no cover - depends on optional extras.
        raise RuntimeError(
            f"{client_name} requires the optional {package_name} package. "
            f"Install it with: pip install -e '.[{extra_name}]'"
        ) from exc
    return getattr(module, symbol_name)


def _strip_json_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def _parse_single_json_object(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    try:
        value, index = decoder.raw_decode(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, Mapping):
        return None
    trailing = text[index:].strip()
    if trailing and trailing != "```":
        return None
    return value


def _last_message(messages: Any) -> Any | None:
    if messages is None:
        return None
    if isinstance(messages, list | tuple):
        if not messages:
            return None
        return messages[-1]
    return messages


def _extract_message_content(message: Any) -> Any:
    if _looks_like_native_tool_call(message):
        raise RuntimeError("framework-native tool calls are not Rowplane intents")
    if isinstance(message, Mapping):
        if "content" in message:
            return message["content"]
        return message
    tool_calls = _get(message, "tool_calls")
    if tool_calls:
        raise RuntimeError("framework-native tool calls are not Rowplane intents")
    content = _get(message, "content")
    if content is not None:
        return content
    return message


def _looks_like_native_tool_call(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "tool_calls" in value or "function_call" in value:
            return True
    if _get(value, "tool_calls") or _get(value, "function_call"):
        return True
    return False


def _find_usage(result: Any) -> Any:
    for candidate in (
        _get(result, "usage"),
        _get(_get(result, "final_response"), "usage"),
        _get(_get(result, "last_response"), "usage"),
    ):
        if candidate is not None:
            return candidate
    raw_responses = _as_list(_get(result, "raw_responses"))
    for response in reversed(raw_responses):
        usage = _get(response, "usage")
        if usage is not None:
            return usage
    return {}


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


def _sum_optional_int(left: Any, right: Any) -> int | None:
    left_int = _int_or_none(left)
    right_int = _int_or_none(right)
    if left_int is None:
        return right_int
    if right_int is None:
        return left_int
    return left_int + right_int


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sum_optional_float(left: Any, right: Any) -> float | None:
    left_float = _float_or_none(left)
    right_float = _float_or_none(right)
    if left_float is None:
        return right_float
    if right_float is None:
        return left_float
    return left_float + right_float
