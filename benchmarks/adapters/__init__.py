"""Benchmark adapter registry."""

from __future__ import annotations

from benchmarks.adapters.base import FrameworkAdapter
from benchmarks.adapters.competitors import (
    CrewAIAdapter,
    LangChainAdapter,
    LangGraphAdapter,
    LlamaIndexAdapter,
    OpenAIAgentsAdapter,
    PydanticAIAdapter,
)
from benchmarks.adapters.rowplane_adapter import RowplaneAdapter
from benchmarks.adapters.plain_openai_adapter import PlainOpenAIToolLoopAdapter


def build_adapters(
    *,
    database_url: str | None = None,
    include_experimental_frameworks: bool = False,
) -> list[FrameworkAdapter]:
    adapters: list[FrameworkAdapter] = [
        RowplaneAdapter(database_url=database_url),
        PlainOpenAIToolLoopAdapter(),
    ]
    if include_experimental_frameworks:
        adapters.extend(build_experimental_framework_adapters())
    return adapters


def build_experimental_framework_adapters() -> list[FrameworkAdapter]:
    """Return non-native framework wrappers for exploratory smoke tests only.

    These adapters intentionally do not claim to represent full native framework
    implementations. They are useful for prompt/tool-loop smoke tests, not for
    product positioning scores.
    """

    return [
        LangGraphAdapter(),
        LangChainAdapter(),
        CrewAIAdapter(),
        PydanticAIAdapter(),
        OpenAIAgentsAdapter(),
        LlamaIndexAdapter(),
    ]


__all__ = ["FrameworkAdapter", "build_adapters", "build_experimental_framework_adapters"]
