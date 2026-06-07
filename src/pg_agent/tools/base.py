"""Tool contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pg_agent.runtime.commands import TOOL_NAME_RE
from pg_agent.runtime.errors import ToolValidationError
from pg_agent.runtime.sanitize import redact_secrets
from pg_agent.runtime.schema import validate_json_schema_subset


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    run_id: str
    tool_name: str
    execution_id: str
    idempotency_key: str
    task_id: str | None = None
    agent_id: str | None = None


@dataclass(frozen=True)
class ToolResult:
    output: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[ToolContext, Mapping[str, Any]], Mapping[str, Any] | ToolResult]
ToolValidator = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class ToolDefinition:
    """A deterministic function made available to workers."""

    name: str
    handler: ToolHandler
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    validator: ToolValidator | None = None
    is_side_effecting: bool = False
    requires_approval: bool = False
    approval_policy: dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def __post_init__(self) -> None:
        if not TOOL_NAME_RE.match(self.name):
            raise ToolValidationError(f"invalid tool name: {self.name}")
        if self.input_schema and self.input_schema.get("type", "object") != "object":
            raise ToolValidationError("tool input_schema must describe an object")
        if self.output_schema and self.output_schema.get("type", "object") != "object":
            raise ToolValidationError("tool output_schema must describe an object")

    def validate_arguments(self, arguments: Mapping[str, Any]) -> None:
        if not isinstance(arguments, Mapping):
            raise ToolValidationError("tool arguments must be an object")
        validate_json_schema_subset(self.input_schema, arguments, subject=f"tool {self.name} arguments")
        if self.validator is not None:
            self.validator(arguments)

    def execute(
        self,
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> ToolResult:
        self.validate_arguments(arguments)
        result = self.handler(context, arguments)
        if isinstance(result, ToolResult):
            output = redact_secrets(result.output)
            validate_json_schema_subset(self.output_schema, output, subject=f"tool {self.name} output")
            return ToolResult(
                output=output,
                metadata=redact_secrets(result.metadata),
            )
        if not isinstance(result, Mapping):
            raise ToolValidationError("tool handler must return an object")
        output = redact_secrets(dict(result))
        validate_json_schema_subset(self.output_schema, output, subject=f"tool {self.name} output")
        return ToolResult(output=output)
