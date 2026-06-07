"""In-process registry for deterministic worker tools."""

from __future__ import annotations

from pg_agent.runtime.errors import ToolNotRegistered
from pg_agent.tools.base import ToolDefinition


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotRegistered(f"tool is not registered in worker: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))
