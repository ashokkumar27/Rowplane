"""Strict planner intent parser for framework-facing adapters.

Intents are proposals from external planners. They are not executable runtime
units. Rowplane validates and records a policy decision before mapping an intent
to the internal command path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from pg_agent.runtime.commands import (
    AskHumanCommand,
    DelegateCommand,
    FailCommand,
    FinalCommand,
    RememberCommand,
    ToolCommand,
)
from pg_agent.runtime.errors import MalformedCommand
from pg_agent.runtime.sanitize import redact_secrets

INTENT_SCHEMA_VERSION = 1
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
INTENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class FinalAnswerIntent:
    schema_version: Literal[1]
    intent: Literal["final_answer"]
    answer: dict[str, Any]
    intent_id: str | None = None


@dataclass(frozen=True)
class ToolRequestIntent:
    schema_version: Literal[1]
    intent: Literal["tool_request"]
    tool_name: str
    arguments: dict[str, Any]
    intent_id: str | None = None


@dataclass(frozen=True)
class ClarificationRequestIntent:
    schema_version: Literal[1]
    intent: Literal["clarification_request"]
    reason: str
    payload: dict[str, Any]
    intent_id: str | None = None


@dataclass(frozen=True)
class MemoryProposalIntent:
    schema_version: Literal[1]
    intent: Literal["memory_proposal"]
    memory_type: str
    content: str
    metadata: dict[str, Any]
    intent_id: str | None = None


@dataclass(frozen=True)
class DelegationRequestIntent:
    schema_version: Literal[1]
    intent: Literal["delegation_request"]
    to_agent: str
    task: dict[str, Any]
    reason: str
    intent_id: str | None = None


@dataclass(frozen=True)
class FailureIntent:
    schema_version: Literal[1]
    intent: Literal["failure"]
    reason: str
    intent_id: str | None = None


RowplaneIntent: TypeAlias = (
    FinalAnswerIntent
    | ToolRequestIntent
    | ClarificationRequestIntent
    | MemoryProposalIntent
    | DelegationRequestIntent
    | FailureIntent
)

REQUIRED_KEYS = {
    "final_answer": {"schema_version", "intent", "answer"},
    "tool_request": {"schema_version", "intent", "tool_name", "arguments"},
    "clarification_request": {"schema_version", "intent", "reason", "payload"},
    "memory_proposal": {"schema_version", "intent", "memory_type", "content", "metadata"},
    "delegation_request": {"schema_version", "intent", "to_agent", "task", "reason"},
    "failure": {"schema_version", "intent", "reason"},
}
OPTIONAL_KEYS = {"intent_id"}


def parse_intent(raw: str | dict[str, Any]) -> RowplaneIntent:
    """Parse and validate exactly one Rowplane planner intent."""

    payload = _load_payload(raw)
    _reject_framework_tool_calls(payload)

    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise MalformedCommand("intent.schema_version must be integer 1")
    if schema_version != INTENT_SCHEMA_VERSION:
        raise MalformedCommand(f"unsupported intent.schema_version: {schema_version}")

    intent = payload.get("intent")
    if not isinstance(intent, str) or intent not in REQUIRED_KEYS:
        raise MalformedCommand("intent is missing or unsupported")

    expected = REQUIRED_KEYS[intent]
    allowed = expected | OPTIONAL_KEYS
    actual = set(payload)
    if not expected.issubset(actual) or not actual.issubset(allowed):
        extra = sorted(actual - allowed)
        missing = sorted(expected - actual)
        raise MalformedCommand(
            f"intent keys do not match contract; missing={missing} extra={extra}"
        )

    intent_id = payload.get("intent_id")
    if intent_id is not None:
        if not isinstance(intent_id, str) or not INTENT_ID_RE.match(intent_id):
            raise MalformedCommand("intent.intent_id must be a stable short string")

    if intent == "final_answer":
        answer = payload["answer"]
        if not isinstance(answer, dict):
            raise MalformedCommand("final_answer.answer must be an object")
        return FinalAnswerIntent(
            schema_version=1,
            intent="final_answer",
            answer=answer,
            intent_id=intent_id,
        )

    if intent == "tool_request":
        tool_name = payload["tool_name"]
        arguments = payload["arguments"]
        if not isinstance(tool_name, str) or not NAME_RE.match(tool_name):
            raise MalformedCommand("tool_request.tool_name must be snake_case")
        if not isinstance(arguments, dict):
            raise MalformedCommand("tool_request.arguments must be an object")
        return ToolRequestIntent(
            schema_version=1,
            intent="tool_request",
            tool_name=tool_name,
            arguments=arguments,
            intent_id=intent_id,
        )

    if intent == "clarification_request":
        reason = payload["reason"]
        clarification_payload = payload["payload"]
        if not isinstance(reason, str) or not reason.strip():
            raise MalformedCommand("clarification_request.reason must be a non-empty string")
        if _contains_approval_decision(reason):
            raise MalformedCommand("adapters must not decide approval requirements")
        if not isinstance(clarification_payload, dict):
            raise MalformedCommand("clarification_request.payload must be an object")
        return ClarificationRequestIntent(
            schema_version=1,
            intent="clarification_request",
            reason=reason,
            payload=clarification_payload,
            intent_id=intent_id,
        )

    if intent == "memory_proposal":
        memory_type = payload["memory_type"]
        content = payload["content"]
        metadata = payload["metadata"]
        if not isinstance(memory_type, str) or not memory_type.strip():
            raise MalformedCommand("memory_proposal.memory_type must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise MalformedCommand("memory_proposal.content must be a non-empty string")
        if not isinstance(metadata, dict):
            raise MalformedCommand("memory_proposal.metadata must be an object")
        return MemoryProposalIntent(
            schema_version=1,
            intent="memory_proposal",
            memory_type=memory_type,
            content=content,
            metadata=metadata,
            intent_id=intent_id,
        )

    if intent == "delegation_request":
        to_agent = payload["to_agent"]
        task = payload["task"]
        reason = payload["reason"]
        if not isinstance(to_agent, str) or not NAME_RE.match(to_agent):
            raise MalformedCommand("delegation_request.to_agent must be snake_case")
        if not isinstance(task, dict):
            raise MalformedCommand("delegation_request.task must be an object")
        if not isinstance(reason, str) or not reason.strip():
            raise MalformedCommand("delegation_request.reason must be a non-empty string")
        return DelegationRequestIntent(
            schema_version=1,
            intent="delegation_request",
            to_agent=to_agent,
            task=task,
            reason=reason,
            intent_id=intent_id,
        )

    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise MalformedCommand("failure.reason must be a non-empty string")
    return FailureIntent(
        schema_version=1,
        intent="failure",
        reason=reason,
        intent_id=intent_id,
    )


def intent_to_event_payload(intent: RowplaneIntent) -> dict[str, Any]:
    payload = redact_secrets(normalize_intent(intent))
    for key in ("answer", "arguments", "payload", "content", "metadata", "task"):
        payload.pop(key, None)
    return payload


def normalize_intent(intent: RowplaneIntent) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in intent.__dict__.items()
        if value is not None
    }
    return payload


def is_intent_payload(raw: str | dict[str, Any]) -> bool:
    try:
        value = _load_payload(raw)
    except MalformedCommand:
        return False
    return "schema_version" in value or "intent" in value


def _intent_to_command(intent: RowplaneIntent) -> (
    FinalCommand
    | ToolCommand
    | AskHumanCommand
    | RememberCommand
    | DelegateCommand
    | FailCommand
):
    """Map a validated intent into the internal command type.

    This is intentionally private. External planners propose intents; Rowplane
    alone decides whether a command may enter the governed execution path.
    """

    if isinstance(intent, FinalAnswerIntent):
        return FinalCommand(action="final", answer=intent.answer)
    if isinstance(intent, ToolRequestIntent):
        return ToolCommand(
            action="tool",
            tool_name=intent.tool_name,
            arguments=intent.arguments,
        )
    if isinstance(intent, ClarificationRequestIntent):
        return AskHumanCommand(
            action="ask_human",
            reason=intent.reason,
            payload=intent.payload,
        )
    if isinstance(intent, MemoryProposalIntent):
        return RememberCommand(
            action="remember",
            memory_type=intent.memory_type,
            content=intent.content,
            metadata=intent.metadata,
        )
    if isinstance(intent, DelegationRequestIntent):
        return DelegateCommand(
            action="delegate",
            to_agent=intent.to_agent,
            task=intent.task,
            reason=intent.reason,
        )
    return FailCommand(action="fail", reason=intent.reason)


def _load_payload(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise MalformedCommand("intent output is not valid JSON") from exc
    elif isinstance(raw, dict):
        value = raw
    else:
        raise MalformedCommand("intent output must be a JSON object")

    if isinstance(value, list):
        raise MalformedCommand("model output must contain exactly one intent object")
    if not isinstance(value, dict):
        raise MalformedCommand("intent output must be exactly one JSON object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedCommand(f"duplicate intent key: {key}")
        result[key] = value
    return result


def _reject_framework_tool_calls(payload: dict[str, Any]) -> None:
    if "tool_calls" in payload or "function_call" in payload:
        raise MalformedCommand("framework-native tool calls are not Rowplane intents")
    if payload.get("intent") in {"human_input", "requires_approval", "approval_required"}:
        raise MalformedCommand("adapters must not decide approval requirements")
    for key in payload:
        if key in {"requires_approval", "approval_required", "approval_status"}:
            raise MalformedCommand("adapters must not decide approval requirements")


def _contains_approval_decision(text: str) -> bool:
    lowered = text.lower()
    return "requires approval" in lowered or "approval required" in lowered
