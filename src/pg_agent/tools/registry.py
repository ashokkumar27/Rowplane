"""In-process registry for deterministic worker tools."""

from __future__ import annotations

from typing import Any

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

    def prompt_contracts(self) -> list[dict[str, Any]]:
        """Return stable tool contracts for model prompt context."""

        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": dict(tool.input_schema or {"type": "object"}),
                "output_schema": dict(tool.output_schema or {"type": "object"}),
                "is_side_effecting": bool(tool.is_side_effecting),
                "requires_approval": bool(tool.requires_approval),
                "approval_policy": dict(tool.approval_policy or {}),
            }
            for tool in (self._tools[name] for name in sorted(self._tools))
        ]
