"""Agent task lifecycle state machine."""

from __future__ import annotations

from enum import StrEnum

from pg_agent.runtime.errors import InvalidStateTransition


class TaskStatus(StrEnum):
    QUEUED = "queued"
    THINKING = "thinking"
    NEEDS_TOOL = "needs_tool"
    TOOL_RUNNING = "tool_running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_CHILD = "waiting_child"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


TERMINAL_TASK_STATUSES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
}

ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.QUEUED: {TaskStatus.THINKING},
    TaskStatus.THINKING: {
        TaskStatus.NEEDS_TOOL,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.WAITING_CHILD,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    },
    TaskStatus.NEEDS_TOOL: {TaskStatus.TOOL_RUNNING},
    TaskStatus.TOOL_RUNNING: {TaskStatus.QUEUED},
    TaskStatus.WAITING_APPROVAL: {TaskStatus.QUEUED},
    TaskStatus.WAITING_CHILD: {TaskStatus.QUEUED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.BLOCKED: set(),
}


def normalize_task_status(status: str | TaskStatus) -> TaskStatus:
    try:
        return status if isinstance(status, TaskStatus) else TaskStatus(status)
    except ValueError as exc:
        raise InvalidStateTransition(f"unknown task status: {status}") from exc


def can_task_transition(current: str | TaskStatus, next_status: str | TaskStatus) -> bool:
    current_status = normalize_task_status(current)
    requested_status = normalize_task_status(next_status)
    if current_status == requested_status:
        return True
    if requested_status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
        return True
    return requested_status in ALLOWED_TASK_TRANSITIONS.get(current_status, set())


def validate_task_transition(current: str | TaskStatus, next_status: str | TaskStatus) -> None:
    if not can_task_transition(current, next_status):
        raise InvalidStateTransition(
            f"invalid agent_task status transition: {current} -> {next_status}"
        )
