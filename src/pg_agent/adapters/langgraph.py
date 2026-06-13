"""LangGraph planner-only intent bridge for Rowplane workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pg_agent.adapters.intent_bridge import (
    DEFAULT_INTENT_BRIDGE_INSTRUCTIONS,
    MessagesInputBuilder,
    OutputExtractor,
    coerce_intent_text,
    combine_usage,
    extract_framework_output,
    extract_usage,
    import_optional_symbol,
    messages_to_langchain_messages,
)


class LangGraphIntentClient:
    """Worker-compatible planner bridge backed by a LangGraph app.

    The graph may plan only. Rowplane tools are not bound to LangGraph, and
    framework-native tool calls are rejected instead of interpreted.
    """

    def __init__(
        self,
        *,
        graph: Any | None = None,
        invoker: Any | None = None,
        instructions: str | None = None,
        invoke_config: Mapping[str, Any] | None = None,
        invoke_options: Mapping[str, Any] | None = None,
        input_builder: MessagesInputBuilder | None = None,
        output_extractor: OutputExtractor | None = None,
        empty_output_retries: int = 1,
        estimated_call_cost_usd: float = 0.0,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ) -> None:
        if graph is None and invoker is None:
            import_optional_symbol(
                "langgraph.graph",
                "StateGraph",
                "langgraph",
                "LangGraphIntentClient",
            )
            raise RuntimeError(
                "LangGraphIntentClient requires a compiled graph/app or invoker. "
                "Pass graph=compiled_graph or invoker=callable."
            )
        self.graph = graph
        self.invoker = invoker
        self.instructions = instructions or DEFAULT_INTENT_BRIDGE_INSTRUCTIONS
        self.invoke_config = dict(invoke_config or {})
        self.invoke_options = dict(invoke_options or {})
        self.input_builder = input_builder or self._default_input_builder
        self.output_extractor = output_extractor or extract_framework_output
        self.empty_output_retries = max(0, int(empty_output_retries))
        self.estimated_call_cost_usd = float(estimated_call_cost_usd) * (self.empty_output_retries + 1)
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.last_usage: dict[str, Any] = {}
        self.last_result: Any | None = None

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        run_input = self.input_builder(messages)
        attempts = self.empty_output_retries + 1
        accumulated_usage: dict[str, Any] = {}
        last_error: RuntimeError | None = None
        for attempt in range(attempts):
            result = self._invoke(run_input)
            self.last_result = result
            usage = extract_usage(
                result,
                input_cost_per_million=self.input_cost_per_million,
                output_cost_per_million=self.output_cost_per_million,
            )
            accumulated_usage = combine_usage(accumulated_usage, usage)
            self.last_usage = accumulated_usage
            try:
                return coerce_intent_text(self.output_extractor(result))
            except RuntimeError as exc:
                if "intent output was empty" not in str(exc) or attempt == attempts - 1:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("LangGraph run did not produce a Rowplane intent")

    def get_last_usage(self) -> Mapping[str, Any]:
        return self.last_usage

    def _invoke(self, run_input: Any) -> Any:
        kwargs = dict(self.invoke_options)
        if self.invoke_config:
            kwargs["config"] = self.invoke_config
        if self.invoker is not None:
            return self.invoker(run_input, **kwargs)
        invoke = getattr(self.graph, "invoke", None)
        if not callable(invoke):
            raise RuntimeError("LangGraph graph must expose invoke(input, **kwargs)")
        return invoke(run_input, **kwargs)

    def _default_input_builder(self, messages: Sequence[Mapping[str, str]]) -> dict[str, Any]:
        return {
            "messages": [("system", self.instructions), *messages_to_langchain_messages(messages)]
        }
