"""Structured runtime errors."""

from __future__ import annotations


class AgentError(Exception):
    """Base error with a stable machine-readable code."""

    code = "agent_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class MalformedCommand(AgentError):
    code = "malformed_command"


class InvalidStateTransition(AgentError):
    code = "invalid_state_transition"


class RunStatusConflict(AgentError):
    code = "run_status_conflict"


class MaxIterationsExceeded(AgentError):
    code = "max_iterations_exceeded"


class ToolNotRegistered(AgentError):
    code = "tool_not_registered"


class ToolPermissionDenied(AgentError):
    code = "tool_permission_denied"


class ToolValidationError(AgentError):
    code = "tool_validation_error"


class ApprovalNotFound(AgentError):
    code = "approval_not_found"


class ApprovalAlreadyResolved(AgentError):
    code = "approval_already_resolved"
