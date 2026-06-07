"""Dynamic final-answer contract validation for agent runs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pg_agent.runtime.errors import ToolValidationError
from pg_agent.runtime.schema import validate_json_schema_subset


@dataclass(frozen=True)
class FinalValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def extract_answer_contract(run: Mapping[str, Any]) -> Mapping[str, Any]:
    task = run.get("task")
    return extract_answer_contract_from_payload(task if isinstance(task, Mapping) else {})


def extract_answer_contract_from_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    contract = payload.get("answer_contract") or payload.get("output_contract")
    if isinstance(contract, Mapping):
        return contract
    schema = payload.get("answer_schema") or payload.get("output_schema")
    if isinstance(schema, Mapping):
        return {"schema": schema}
    return {}


def validate_final_answer(
    answer: Mapping[str, Any],
    contract: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> FinalValidationResult:
    """Validate a model final answer against an optional run contract.

    The contract is intentionally data-driven. The model may choose any route to
    the answer, but the harness can require final shape and evidence that must
    already exist in the append-only trajectory.
    """

    if not contract:
        return FinalValidationResult(True)

    errors: list[str] = []
    schema = contract.get("schema") or contract.get("answer_schema")
    if isinstance(schema, Mapping):
        try:
            validate_json_schema_subset(schema, answer, subject="final.answer")
        except ToolValidationError as exc:
            errors.append(str(exc))

    required_event_types = _string_list(contract.get("required_event_types"))
    event_types = [str(event.get("event_type")) for event in events]
    missing_events = [event_type for event_type in required_event_types if event_type not in event_types]
    if missing_events:
        errors.append(f"missing required event evidence: {missing_events}")

    required_tools = _string_list(contract.get("required_tools"))
    if required_tools:
        completed_tools = _completed_tools(events)
        missing_tools = [tool for tool in required_tools if tool not in completed_tools]
        if missing_tools:
            errors.append(f"missing required completed tool evidence: {missing_tools}")

    if contract.get("must_reference_tools") is True:
        completed_tools = _completed_tools(events)
        references = _tool_references(answer)
        missing_refs = sorted(completed_tools - references)
        if completed_tools and missing_refs:
            errors.append(f"final answer does not reference completed tools: {missing_refs}")

    approval_status = contract.get("required_approval_status")
    if isinstance(approval_status, str):
        statuses = _approval_statuses(events)
        if approval_status not in statuses:
            errors.append(f"missing approval status evidence: {approval_status}")

    min_tool_successes = contract.get("min_tool_successes")
    if min_tool_successes is not None:
        if len(_completed_tools(events)) < int(min_tool_successes):
            errors.append(f"expected at least {min_tool_successes} completed tools")

    return FinalValidationResult(valid=not errors, errors=errors)


def _completed_tools(events: Sequence[Mapping[str, Any]]) -> set[str]:
    tools: set[str] = set()
    for event in events:
        if event.get("event_type") != "tool_completed":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping) and isinstance(payload.get("tool_name"), str):
            tools.add(str(payload["tool_name"]))
    return tools


def _approval_statuses(events: Sequence[Mapping[str, Any]]) -> set[str]:
    statuses: set[str] = set()
    for event in events:
        if event.get("event_type") != "approval_resolved":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping) and isinstance(payload.get("status"), str):
            statuses.add(str(payload["status"]))
    return statuses


def _tool_references(value: Any) -> set[str]:
    references: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"tool_name", "tool", "tools", "evidence_tools"}:
                references.update(_strings_in(item))
            references.update(_tool_references(item))
    elif isinstance(value, list):
        for item in value:
            references.update(_tool_references(item))
    return references


def _strings_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        result: set[str] = set()
        for item in value:
            result.update(_strings_in(item))
        return result
    if isinstance(value, Mapping):
        result: set[str] = set()
        for item in value.values():
            result.update(_strings_in(item))
        return result
    return set()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
