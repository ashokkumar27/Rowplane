"""Agent run lifecycle state machine."""

from __future__ import annotations

from enum import StrEnum

from pg_agent.runtime.errors import InvalidStateTransition


class RunStatus(StrEnum):
    QUEUED = "queued"
    THINKING = "thinking"
    NEEDS_TOOL = "needs_tool"
    TOOL_RUNNING = "tool_running"
    WAITING_APPROVAL = "waiting_approval"
    EVALUATING = "evaluating"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.BLOCKED,
}

ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.QUEUED: {RunStatus.THINKING},
    RunStatus.THINKING: {
        RunStatus.NEEDS_TOOL,
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
    },
    RunStatus.NEEDS_TOOL: {RunStatus.TOOL_RUNNING},
    RunStatus.TOOL_RUNNING: {RunStatus.QUEUED},
    RunStatus.WAITING_APPROVAL: {RunStatus.QUEUED},
    RunStatus.EVALUATING: set(),
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: set(),
    RunStatus.BLOCKED: set(),
}


def normalize_status(status: str | RunStatus) -> RunStatus:
    try:
        return status if isinstance(status, RunStatus) else RunStatus(status)
    except ValueError as exc:
        raise InvalidStateTransition(f"unknown run status: {status}") from exc


def can_transition(current: str | RunStatus, next_status: str | RunStatus) -> bool:
    current_status = normalize_status(current)
    requested_status = normalize_status(next_status)
    if current_status == requested_status:
        return True
    if requested_status in {RunStatus.FAILED, RunStatus.BLOCKED}:
        return True
    return requested_status in ALLOWED_TRANSITIONS.get(current_status, set())


def validate_transition(current: str | RunStatus, next_status: str | RunStatus) -> None:
    if not can_transition(current, next_status):
        raise InvalidStateTransition(
            f"invalid agent_run status transition: {current} -> {next_status}"
        )
