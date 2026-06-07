"""Shared benchmark data contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    side_effecting: bool = False
    requires_approval: bool = False
    tenant_scoped: bool = True


@dataclass(frozen=True)
class BenchmarkScenario:
    name: str
    prompt: str
    expected: dict[str, Any]
    tools: tuple[ToolSpec, ...]
    tags: tuple[str, ...] = ()
    max_turns: int = 8


@dataclass
class ToolCallRecord:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    approved: bool | None = None
    side_effect_committed: bool = False


@dataclass
class BenchmarkRunRecord:
    framework: str
    scenario: str
    repeat: int
    model: str
    answer: dict[str, Any] | None = None
    final_text: str | None = None
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[dict[str, Any]] = field(default_factory=list)
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    sql_evidence: list[dict[str, Any]] = field(default_factory=list)
    retrieval_evidence: list[dict[str, Any]] = field(default_factory=list)
    tenant_leaks: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    implementation_notes: list[str] = field(default_factory=list)
    score: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
