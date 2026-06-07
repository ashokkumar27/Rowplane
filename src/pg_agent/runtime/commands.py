"""Strict command parser for model output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from pg_agent.runtime.errors import MalformedCommand
from pg_agent.runtime.sanitize import redact_secrets

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class FinalCommand:
    action: Literal["final"]
    answer: dict[str, Any]


@dataclass(frozen=True)
class ToolCommand:
    action: Literal["tool"]
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AskHumanCommand:
    action: Literal["ask_human"]
    reason: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class RememberCommand:
    action: Literal["remember"]
    memory_type: str
    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class DelegateCommand:
    action: Literal["delegate"]
    to_agent: str
    task: dict[str, Any]
    reason: str


@dataclass(frozen=True)
class FailCommand:
    action: Literal["fail"]
    reason: str


AgentCommand: TypeAlias = (
    FinalCommand
    | ToolCommand
    | AskHumanCommand
    | RememberCommand
    | DelegateCommand
    | FailCommand
)

REQUIRED_KEYS = {
    "final": {"action", "answer"},
    "tool": {"action", "tool_name", "arguments"},
    "ask_human": {"action", "reason", "payload"},
    "remember": {"action", "memory_type", "content", "metadata"},
    "delegate": {"action", "to_agent", "task", "reason"},
    "fail": {"action", "reason"},
}


def parse_command(raw: str | dict[str, Any]) -> AgentCommand:
    """Parse and validate exactly one model command."""

    payload = _load_payload(raw)
    action = payload.get("action")
    if not isinstance(action, str) or action not in REQUIRED_KEYS:
        raise MalformedCommand("command action is missing or unsupported")

    expected_keys = REQUIRED_KEYS[action]
    actual_keys = set(payload)
    if actual_keys != expected_keys:
        extra = sorted(actual_keys - expected_keys)
        missing = sorted(expected_keys - actual_keys)
        raise MalformedCommand(
            f"command keys do not match contract; missing={missing} extra={extra}"
        )

    if action == "final":
        answer = payload["answer"]
        if not isinstance(answer, dict):
            raise MalformedCommand("final.answer must be an object")
        return FinalCommand(action="final", answer=answer)

    if action == "tool":
        tool_name = payload["tool_name"]
        arguments = payload["arguments"]
        if not isinstance(tool_name, str) or not TOOL_NAME_RE.match(tool_name):
            raise MalformedCommand("tool.tool_name must be snake_case")
        if not isinstance(arguments, dict):
            raise MalformedCommand("tool.arguments must be an object")
        return ToolCommand(action="tool", tool_name=tool_name, arguments=arguments)

    if action == "ask_human":
        reason = payload["reason"]
        approval_payload = payload["payload"]
        if not isinstance(reason, str) or not reason.strip():
            raise MalformedCommand("ask_human.reason must be a non-empty string")
        if not isinstance(approval_payload, dict):
            raise MalformedCommand("ask_human.payload must be an object")
        return AskHumanCommand(
            action="ask_human",
            reason=reason,
            payload=approval_payload,
        )

    if action == "remember":
        memory_type = payload["memory_type"]
        content = payload["content"]
        metadata = payload["metadata"]
        if not isinstance(memory_type, str) or not memory_type.strip():
            raise MalformedCommand("remember.memory_type must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise MalformedCommand("remember.content must be a non-empty string")
        if not isinstance(metadata, dict):
            raise MalformedCommand("remember.metadata must be an object")
        return RememberCommand(
            action="remember",
            memory_type=memory_type,
            content=content,
            metadata=metadata,
        )

    if action == "delegate":
        to_agent = payload["to_agent"]
        task = payload["task"]
        reason = payload["reason"]
        if not isinstance(to_agent, str) or not TOOL_NAME_RE.match(to_agent):
            raise MalformedCommand("delegate.to_agent must be snake_case")
        if not isinstance(task, dict):
            raise MalformedCommand("delegate.task must be an object")
        if not isinstance(reason, str) or not reason.strip():
            raise MalformedCommand("delegate.reason must be a non-empty string")
        return DelegateCommand(
            action="delegate",
            to_agent=to_agent,
            task=task,
            reason=reason,
        )

    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise MalformedCommand("fail.reason must be a non-empty string")
    return FailCommand(action="fail", reason=reason)


def command_to_event_payload(command: AgentCommand) -> dict[str, Any]:
    return redact_secrets(command.__dict__)


def _load_payload(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise MalformedCommand("model output is not valid JSON") from exc
    elif isinstance(raw, dict):
        value = raw
    else:
        raise MalformedCommand("model output must be a JSON object")

    if not isinstance(value, dict):
        raise MalformedCommand("model output must be exactly one JSON object")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MalformedCommand(f"duplicate command key: {key}")
        result[key] = value
    return result
