"""Task-scoped tool execution for multi-agent workers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pg_agent.runtime.commands import ToolCommand
from pg_agent.runtime.errors import (
    AgentError,
    ToolNotRegistered,
    ToolPermissionDenied,
    ToolValidationError,
)
from pg_agent.runtime.sanitize import redact_secrets, stable_hash
from pg_agent.tools.base import ToolContext
from pg_agent.tools.executor import ToolExecutionOutcome
from pg_agent.tools.registry import ToolRegistry


class TaskToolExecutionRepository(Protocol):
    def append_event(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "worker",
    ) -> None: ...

    def update_task_status(
        self,
        task_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]: ...

    def queue_task(self, tenant_id: str, run_id: str, task_id: str) -> None: ...

    def get_agent_tool(self, tenant_id: str, tool_name: str) -> Mapping[str, Any] | None: ...

    def has_tool_permission(
        self,
        tenant_id: str,
        tool_id: str,
        run_id: str,
        *,
        agent_id: str | None = None,
    ) -> bool: ...

    def get_tool_execution_by_key(
        self,
        tenant_id: str,
        tool_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None: ...

    def create_tool_execution(
        self,
        tenant_id: str,
        run_id: str,
        tool_id: str,
        idempotency_key: str,
        arguments: Mapping[str, Any],
        arguments_hash: str,
        *,
        task_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def update_tool_execution(
        self,
        execution_id: str,
        status: str,
        **fields: Any,
    ) -> Mapping[str, Any]: ...

    def create_approval_request(
        self,
        tenant_id: str,
        run_id: str,
        reason: str,
        payload: Mapping[str, Any],
        *,
        tool_execution_id: str | None = None,
        task_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def get_approval_for_execution(self, execution_id: str) -> Mapping[str, Any] | None: ...

    def reserve_tool_execution(
        self,
        tenant_id: str,
        run_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        force_approval: bool = False,
        actor: str = "worker",
    ) -> Mapping[str, Any]: ...

    def complete_tool_execution(
        self,
        execution_id: str,
        *,
        succeeded: bool,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class TaskToolExecutor:
    repository: TaskToolExecutionRepository
    registry: ToolRegistry

    def execute(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        agent: Mapping[str, Any],
        command: ToolCommand,
    ) -> ToolExecutionOutcome:
        tenant_id = str(run["tenant_id"])
        run_id = str(run["id"])
        task_id = str(task["id"])
        agent_id = str(agent["id"])
        current_status = str(task["status"])

        local_tool = self.registry.get(command.tool_name)
        if hasattr(self.repository, "reserve_tool_execution") and hasattr(self.repository, "complete_tool_execution"):
            return self._execute_with_database_reservation(run, task, agent, command, local_tool)

        db_tool = self.repository.get_agent_tool(tenant_id, command.tool_name)
        if db_tool is None or not db_tool.get("enabled", False):
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_rejected",
                {"task_id": task_id, "agent_id": agent_id, "tool_name": command.tool_name},
            )
            raise ToolNotRegistered(f"tool is not registered in agent_tools: {command.tool_name}")

        tool_id = str(db_tool["id"])
        if not self.repository.has_tool_permission(
            tenant_id,
            tool_id,
            run_id,
            agent_id=agent_id,
        ):
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_permission_denied",
                {"task_id": task_id, "agent_id": agent_id, "tool_name": command.tool_name, "tool_id": tool_id},
            )
            raise ToolPermissionDenied(f"permission denied for tool: {command.tool_name}")

        try:
            local_tool.validate_arguments(command.arguments)
        except ToolValidationError:
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_validation_failed",
                {"task_id": task_id, "agent_id": agent_id, "tool_name": command.tool_name},
            )
            raise

        arguments = redact_secrets(command.arguments)
        arguments_hash = stable_hash(arguments)
        idempotency_key = stable_hash(
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "task_id": task_id,
                "tool_name": command.tool_name,
                "arguments_hash": arguments_hash,
            }
        )
        execution = self.repository.get_tool_execution_by_key(tenant_id, tool_id, idempotency_key)
        if execution is None:
            execution = self.repository.create_tool_execution(
                tenant_id,
                run_id,
                tool_id,
                idempotency_key,
                arguments,
                arguments_hash,
                task_id=task_id,
            )

        approval = self.repository.get_approval_for_execution(str(execution["id"]))
        requires_approval = bool(db_tool.get("requires_approval")) or local_tool.requires_approval
        if requires_approval and approval is None:
            approval = self.repository.create_approval_request(
                tenant_id,
                run_id,
                f"Approval required to execute tool {command.tool_name}.",
                {"tool_name": command.tool_name, "arguments": arguments, "idempotency_key": idempotency_key, "task_id": task_id},
                tool_execution_id=str(execution["id"]),
                task_id=task_id,
            )
            self.repository.update_tool_execution(str(execution["id"]), "waiting_approval")
            self.repository.append_event(
                tenant_id,
                run_id,
                "approval_requested",
                {"task_id": task_id, "agent_id": agent_id, "approval_request_id": str(approval["id"]), "tool_execution_id": str(execution["id"]), "tool_name": command.tool_name},
            )
            self.repository.update_task_status(task_id, current_status, "waiting_approval")
            return ToolExecutionOutcome("waiting_approval", str(execution["id"]), str(approval["id"]))

        status = str(execution["status"])
        if status == "completed":
            self._advance_tool_states(task_id, current_status)
            result = dict(execution.get("result") or {})
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_execution_replayed",
                {"task_id": task_id, "agent_id": agent_id, "tool_execution_id": str(execution["id"]), "tool_name": command.tool_name, "result": result},
            )
            self.repository.update_task_status(task_id, "tool_running", "queued")
            self.repository.queue_task(tenant_id, run_id, task_id)
            return ToolExecutionOutcome("completed", str(execution["id"]), result=result)

        if approval is not None and approval.get("status") == "rejected":
            self.repository.append_event(
                tenant_id,
                run_id,
                "approval_rejected",
                {"task_id": task_id, "agent_id": agent_id, "approval_request_id": str(approval["id"]), "tool_execution_id": str(execution["id"])},
            )
            self.repository.update_task_status(task_id, current_status, "blocked")
            return ToolExecutionOutcome("blocked", str(execution["id"]), str(approval["id"]))

        if requires_approval and approval.get("status") != "approved":
            self.repository.update_task_status(task_id, current_status, "waiting_approval")
            return ToolExecutionOutcome("waiting_approval", str(execution["id"]), str(approval["id"]))

        self._advance_tool_states(task_id, current_status)
        self.repository.update_tool_execution(str(execution["id"]), "running")
        self.repository.append_event(
            tenant_id,
            run_id,
            "tool_started",
            {"task_id": task_id, "agent_id": agent_id, "tool_execution_id": str(execution["id"]), "tool_name": command.tool_name, "arguments": arguments},
        )
        context = ToolContext(
            tenant_id=tenant_id,
            run_id=run_id,
            tool_name=command.tool_name,
            execution_id=str(execution["id"]),
            idempotency_key=idempotency_key,
            task_id=task_id,
            agent_id=agent_id,
        )
        try:
            result = local_tool.execute(context, command.arguments)
        except Exception as exc:
            self.repository.update_tool_execution(str(execution["id"]), "failed", error=str(exc))
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_failed",
                {"task_id": task_id, "agent_id": agent_id, "tool_execution_id": str(execution["id"]), "tool_name": command.tool_name, "error": str(exc)},
            )
            self.repository.update_task_status(task_id, "tool_running", "queued")
            self.repository.queue_task(tenant_id, run_id, task_id)
            return ToolExecutionOutcome("failed", str(execution["id"]))

        event_result = {"output": result.output, "metadata": result.metadata}
        self.repository.update_tool_execution(str(execution["id"]), "completed", result=event_result)
        self.repository.append_event(
            tenant_id,
            run_id,
            "tool_completed",
            {"task_id": task_id, "agent_id": agent_id, "tool_execution_id": str(execution["id"]), "tool_name": command.tool_name, "result": event_result},
        )
        self.repository.update_task_status(task_id, "tool_running", "queued")
        self.repository.queue_task(tenant_id, run_id, task_id)
        return ToolExecutionOutcome("completed", str(execution["id"]), result=event_result)

    def _execute_with_database_reservation(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        agent: Mapping[str, Any],
        command: ToolCommand,
        local_tool: Any,
    ) -> ToolExecutionOutcome:
        tenant_id = str(run["tenant_id"])
        run_id = str(run["id"])
        task_id = str(task["id"])
        agent_id = str(agent["id"])

        try:
            local_tool.validate_arguments(command.arguments)
        except ToolValidationError:
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_validation_failed",
                {"task_id": task_id, "agent_id": agent_id, "tool_name": command.tool_name},
            )
            raise

        arguments = redact_secrets(command.arguments)
        decision = self.repository.reserve_tool_execution(
            tenant_id,
            run_id,
            command.tool_name,
            arguments,
            task_id=task_id,
            agent_id=agent_id,
            force_approval=bool(local_tool.requires_approval),
        )
        decision_name = str(decision.get("decision"))
        execution_id = str(decision["tool_execution_id"]) if decision.get("tool_execution_id") else None
        approval_request_id = (
            str(decision["approval_request_id"]) if decision.get("approval_request_id") else None
        )

        if decision_name == "execute_tool":
            if execution_id is None:
                raise RuntimeError("database returned execute_tool without tool_execution_id")
            context = ToolContext(
                tenant_id=tenant_id,
                run_id=run_id,
                tool_name=command.tool_name,
                execution_id=execution_id,
                idempotency_key=str(decision["idempotency_key"]),
                task_id=task_id,
                agent_id=agent_id,
            )
            try:
                result = local_tool.execute(context, command.arguments)
            except Exception as exc:
                self.repository.complete_tool_execution(
                    execution_id,
                    succeeded=False,
                    error=str(exc),
                )
                return ToolExecutionOutcome("failed", execution_id)

            event_result = {"output": result.output, "metadata": result.metadata}
            self.repository.complete_tool_execution(
                execution_id,
                succeeded=True,
                result=event_result,
            )
            return ToolExecutionOutcome("completed", execution_id, result=event_result)

        if decision_name == "waiting_approval":
            return ToolExecutionOutcome("waiting_approval", execution_id, approval_request_id)
        if decision_name in {"replayed", "in_progress"}:
            return ToolExecutionOutcome(
                str(decision.get("status", "completed")),
                execution_id,
                result=dict(decision.get("result") or {}),
            )
        if decision_name == "blocked":
            return ToolExecutionOutcome("blocked", execution_id, approval_request_id)
        if decision_name == "permission_denied":
            raise ToolPermissionDenied(f"permission denied for tool: {command.tool_name}")
        if decision_name == "validation_failed":
            raise ToolValidationError(f"tool arguments failed database schema validation: {command.tool_name}")
        if decision_name in {"rejected", "missing_run", "missing_task"}:
            raise ToolNotRegistered(f"tool is not registered in agent_tools: {command.tool_name}")
        raise RuntimeError(f"unsupported database tool decision: {decision}")

    def _advance_tool_states(self, task_id: str, current_status: str) -> None:
        self.repository.update_task_status(task_id, current_status, "needs_tool")
        self.repository.update_task_status(task_id, "needs_tool", "tool_running")


def fail_task_for_tool_error(
    repository: TaskToolExecutionRepository,
    run: Mapping[str, Any],
    task: Mapping[str, Any],
    error: AgentError,
) -> None:
    tenant_id = str(run["tenant_id"])
    run_id = str(run["id"])
    task_id = str(task["id"])
    repository.append_event(
        tenant_id,
        run_id,
        "task_failed",
        {"task_id": task_id, "code": error.code, "error": str(error)},
    )
    repository.update_task_status(task_id, str(task["status"]), "failed", error=str(error))
