"""Prompt construction at the model boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pg_agent.runtime.sanitize import redact_secrets


SYSTEM_PROMPT = """You are an agent command proposer.
Return exactly one JSON object and no prose.
Allowed actions are final, tool, ask_human, remember, and fail.
You may propose a tool call, but workers execute tools only after validation.
If the run task includes answer_contract, the final.answer object must satisfy it and cite required evidence.
If the event history already contains enough tool, memory, or approval evidence to answer the task, return final instead of repeating a tool call.
If a final_answer_rejected event exists, correct the final answer using its errors instead of repeating the same invalid answer.
Never include secrets in arguments, answers, metadata, or reasons."""


def build_agent_prompt(
    run: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Build a simple chat prompt from database state."""

    state = {
        "run": redact_secrets(dict(run)),
        "events": [redact_secrets(dict(event)) for event in events],
        "command_contract": {
            "final": {"action": "final", "answer": {}},
            "tool": {
                "action": "tool",
                "tool_name": "registered_tool_name",
                "arguments": {},
            },
            "ask_human": {
                "action": "ask_human",
                "reason": "Approval or input required.",
                "payload": {},
            },
            "remember": {
                "action": "remember",
                "memory_type": "case_learning",
                "content": "Useful memory text.",
                "metadata": {},
            },
            "fail": {"action": "fail", "reason": "Cannot continue."},
        },
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(state, sort_keys=True, default=str)},
    ]



TASK_SYSTEM_PROMPT = """You are one specialist agent in a governed multi-agent harness.
Return exactly one JSON object and no prose.
Allowed actions are final, tool, ask_human, remember, delegate, and fail.
Delegation creates a bounded child task; do not simulate other agents yourself.
You may propose a tool call, but workers execute tools only after validation.
If the task input includes answer_contract, the final.answer object must satisfy it and cite required evidence.
If the event and message history already contains enough tool, child-task, memory, or approval evidence to answer your task, return final instead of repeating a tool call or delegation.
If a final_answer_rejected event exists, correct the final answer using its errors instead of repeating the same invalid answer.
Never include secrets in arguments, answers, metadata, or reasons."""


def build_agent_task_prompt(
    run: Mapping[str, Any],
    task: Mapping[str, Any],
    agent: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    state = {
        "run": redact_secrets(dict(run)),
        "task": redact_secrets(dict(task)),
        "agent": redact_secrets(dict(agent)),
        "messages": [redact_secrets(dict(message)) for message in messages],
        "events": [redact_secrets(dict(event)) for event in events],
        "command_contract": {
            "final": {"action": "final", "answer": {}},
            "tool": {
                "action": "tool",
                "tool_name": "registered_tool_name",
                "arguments": {},
            },
            "ask_human": {
                "action": "ask_human",
                "reason": "Approval or input required.",
                "payload": {},
            },
            "remember": {
                "action": "remember",
                "memory_type": "case_learning",
                "content": "Useful memory text.",
                "metadata": {},
            },
            "delegate": {
                "action": "delegate",
                "to_agent": "researcher",
                "task": {},
                "reason": "Need bounded specialist work.",
            },
            "fail": {"action": "fail", "reason": "Cannot continue."},
        },
    }
    return [
        {"role": "system", "content": TASK_SYSTEM_PROMPT},
        {"role": "system", "content": str(agent.get("instructions", ""))},
        {"role": "user", "content": json.dumps(state, sort_keys=True, default=str)},
    ]
