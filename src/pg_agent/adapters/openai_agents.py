"""OpenAI Agents SDK command bridge for Rowplane workers.

This adapter intentionally uses OpenAI Agents as a command proposer only. It
returns exactly one Rowplane command string to the worker; Rowplane still owns
schema validation, approvals, idempotency, queueing, events, and tool execution.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

DEFAULT_OPENAI_AGENTS_MODEL = "gpt-5.4-mini"

DEFAULT_COMMAND_BRIDGE_INSTRUCTIONS = """You are a Rowplane command proposer.
Return exactly one JSON object and no prose.
Use the registered_tools data in the prompt state to choose tool names and arguments.
Do not execute tools yourself. Do not call external APIs yourself.
If a tool requires approval, still return the tool command; Rowplane will pause the run and create the approval request.
After approval is resolved, repeat the same tool command so Rowplane can execute the approved side effect idempotently.
If required evidence is missing, return a tool or remember command before final.
Never include secrets in arguments, answers, metadata, or reasons."""

MessagesInputBuilder = Callable[[Sequence[Mapping[str, str]]], Any]


class OpenAIAgentsCommandClient:
    """Worker-compatible model client backed by the OpenAI Agents SDK.

    The adapter is a bridge, not a tool runtime. The Agents SDK may plan the next
    action, but the returned value must be one Rowplane command JSON object.
    """

    def __init__(
        self,
        *,
        agent: Any | None = None,
        runner: Any | None = None,
        model: str = DEFAULT_OPENAI_AGENTS_MODEL,
        name: str = "Rowplane command planner",
        instructions: str | None = None,
        run_config: Any | None = None,
        max_turns: int | None = 1,
        model_settings: Any | None = None,
        max_output_tokens: int | None = 1200,
        runner_options: Mapping[str, Any] | None = None,
        input_builder: MessagesInputBuilder | None = None,
        empty_output_retries: int = 1,
        estimated_call_cost_usd: float = 0.0,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ) -> None:
        self.model = model
        self.instructions = instructions or DEFAULT_COMMAND_BRIDGE_INSTRUCTIONS
        self.run_config = run_config
        self.max_turns = max_turns
        self.runner_options = dict(runner_options or {})
        self.input_builder = input_builder or messages_to_agent_input
        self.empty_output_retries = max(0, int(empty_output_retries))
        self.estimated_call_cost_usd = float(estimated_call_cost_usd) * (self.empty_output_retries + 1)
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.last_usage: dict[str, Any] = {}
        self.last_result: Any | None = None

        if agent is None:
            Agent = _import_agents_symbol("Agent")
            if model_settings is None:
                model_settings = _default_model_settings(max_output_tokens=max_output_tokens)
            agent = Agent(
                name=name,
                instructions=self.instructions,
                model=model,
                model_settings=model_settings,
            )
        self.agent = agent

        if runner is None:
            runner = _import_agents_symbol("Runner")
        self.runner = runner

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        run_input = self.input_builder(messages)
        kwargs = dict(self.runner_options)
        if self.run_config is not None:
            kwargs["run_config"] = self.run_config
        if self.max_turns is not None:
            kwargs["max_turns"] = self.max_turns

        attempts = self.empty_output_retries + 1
        accumulated_usage: dict[str, Any] = {}
        last_error: RuntimeError | None = None
        for attempt in range(attempts):
            result = self.runner.run_sync(self.agent, run_input, **kwargs)
            self.last_result = result
            usage = extract_agents_usage(
                result,
                input_cost_per_million=self.input_cost_per_million,
                output_cost_per_million=self.output_cost_per_million,
            )
            accumulated_usage = combine_usage(accumulated_usage, usage)
            self.last_usage = accumulated_usage
            try:
                return coerce_command_text(extract_final_output(result))
            except RuntimeError as exc:
                if "final output was empty" not in str(exc) or attempt == attempts - 1:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("OpenAI Agents run did not produce a command")

    def get_last_usage(self) -> Mapping[str, Any]:
        return self.last_usage


def messages_to_agent_input(messages: Sequence[Mapping[str, str]]) -> str:
    """Convert Rowplane worker messages to one Agents SDK input string."""

    parts: list[str] = []
    for message in messages:
        role = str(message.get("role", "user")).upper()
        content = str(message.get("content", ""))
        parts.append(f"{role}:\n{content}")
    return "\n\n".join(parts)


def extract_final_output(result: Any) -> Any:
    if isinstance(result, str):
        return result
    for key in ("final_output", "output_text", "content"):
        value = _get(result, key)
        if value is not None:
            return value
    if isinstance(result, Mapping) and "output" in result:
        return result["output"]
    raise RuntimeError("OpenAI Agents run did not contain final output")


def coerce_command_text(output: Any) -> str:
    if isinstance(output, str):
        text = normalize_command_text(output)
        if not text:
            raise RuntimeError("OpenAI Agents final output was empty")
        return text

    model_dump = getattr(output, "model_dump", None)
    if callable(model_dump):
        output = model_dump()
    elif hasattr(output, "dict") and callable(getattr(output, "dict")):
        output = output.dict()

    if isinstance(output, Mapping | list | tuple):
        return json.dumps(output, sort_keys=True, default=str)

    raise RuntimeError(
        "OpenAI Agents final output must be a JSON string, mapping, sequence, or Pydantic-like object"
    )


def normalize_command_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    unfenced = _strip_json_fence(stripped)
    parsed = _parse_json_object(unfenced)
    if parsed is not None:
        return json.dumps(parsed, sort_keys=True, default=str)

    start = unfenced.find("{")
    if start >= 0:
        parsed = _parse_json_object(unfenced[start:])
        if parsed is not None:
            return json.dumps(parsed, sort_keys=True, default=str)

    return stripped


def _strip_json_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def _parse_json_object(text: str) -> Any | None:
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


def extract_agents_usage(
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
    estimated_cost = _estimate_cost(
        input_tokens,
        output_tokens,
        input_cost_per_million=input_cost_per_million,
        output_cost_per_million=output_cost_per_million,
    )
    if estimated_cost is not None:
        output["estimated_cost_usd"] = estimated_cost
    return output


def combine_usage(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _sum_optional_int(left.get(key), right.get(key))
        result[key] = value
    estimated = _sum_optional_float(left.get("estimated_cost_usd"), right.get("estimated_cost_usd"))
    if estimated is not None:
        result["estimated_cost_usd"] = estimated
    return result


def _sum_optional_int(left: Any, right: Any) -> int | None:
    left_int = _int_or_none(left)
    right_int = _int_or_none(right)
    if left_int is None:
        return right_int
    if right_int is None:
        return left_int
    return left_int + right_int


def _sum_optional_float(left: Any, right: Any) -> float | None:
    left_float = _float_or_none(left)
    right_float = _float_or_none(right)
    if left_float is None:
        return right_float
    if right_float is None:
        return left_float
    return left_float + right_float


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


def _default_model_settings(*, max_output_tokens: int | None) -> Any:
    ModelSettings = _import_agents_symbol("ModelSettings")
    kwargs: dict[str, Any] = {"verbosity": "low", "include_usage": True}
    if max_output_tokens is not None:
        kwargs["max_tokens"] = max_output_tokens
    return ModelSettings(**kwargs)


def _import_agents_symbol(name: str) -> Any:
    try:
        module = __import__("agents", fromlist=[name])
    except ImportError as exc:  # pragma: no cover - depends on optional extra availability.
        raise RuntimeError(
            "OpenAIAgentsCommandClient requires the optional OpenAI Agents SDK. "
            "Install it with: pip install -e '.[openai-agents]'"
        ) from exc
    return getattr(module, name)


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


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
