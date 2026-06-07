"""Deterministic tool execution with registration, permission, and approval checks."""

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
from pg_agent.tools.registry import ToolRegistry


class ToolExecutionRepository(Protocol):
    def append_event(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "worker",
    ) -> None: ...

    def update_run_status(
        self,
        run_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]: ...

    def queue_run(self, tenant_id: str, run_id: str) -> None: ...

    def get_agent_tool(
        self,
        tenant_id: str,
        tool_name: str,
    ) -> Mapping[str, Any] | None: ...

    def has_tool_permission(
        self,
        tenant_id: str,
        tool_id: str,
        run_id: str,
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
    ) -> Mapping[str, Any]: ...

    def get_approval_for_execution(
        self,
        execution_id: str,
    ) -> Mapping[str, Any] | None: ...

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
class ToolExecutionOutcome:
    status: str
    execution_id: str | None = None
    approval_request_id: str | None = None
    result: dict[str, Any] | None = None


class ToolExecutor:
    def __init__(
        self,
        repository: ToolExecutionRepository,
        registry: ToolRegistry,
    ) -> None:
        self.repository = repository
        self.registry = registry

    def execute(
        self,
        run: Mapping[str, Any],
        command: ToolCommand,
    ) -> ToolExecutionOutcome:
        tenant_id = str(run["tenant_id"])
        run_id = str(run["id"])
        current_status = str(run["status"])

        local_tool = self.registry.get(command.tool_name)
        if hasattr(self.repository, "reserve_tool_execution") and hasattr(self.repository, "complete_tool_execution"):
            return self._execute_with_database_reservation(run, command, local_tool)

        db_tool = self.repository.get_agent_tool(tenant_id, command.tool_name)
        if db_tool is None or not db_tool.get("enabled", False):
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_rejected",
                {"tool_name": command.tool_name, "reason": "not_registered_or_disabled"},
            )
            raise ToolNotRegistered(f"tool is not registered in agent_tools: {command.tool_name}")

        tool_id = str(db_tool["id"])
        if not self.repository.has_tool_permission(tenant_id, tool_id, run_id):
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_permission_denied",
                {"tool_name": command.tool_name, "tool_id": tool_id},
            )
            raise ToolPermissionDenied(f"permission denied for tool: {command.tool_name}")

        try:
            local_tool.validate_arguments(command.arguments)
        except ToolValidationError:
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_validation_failed",
                {"tool_name": command.tool_name},
            )
            raise

        arguments = redact_secrets(command.arguments)
        arguments_hash = stable_hash(arguments)
        idempotency_key = stable_hash(
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "tool_name": command.tool_name,
                "arguments_hash": arguments_hash,
            }
        )
        execution = self.repository.get_tool_execution_by_key(
            tenant_id,
            tool_id,
            idempotency_key,
        )
        if execution is None:
            execution = self.repository.create_tool_execution(
                tenant_id,
                run_id,
                tool_id,
                idempotency_key,
                arguments,
                arguments_hash,
            )

        requires_approval = (
            bool(db_tool.get("requires_approval"))
            or local_tool.requires_approval
            or _approval_policy_requires_approval(db_tool.get("approval_policy") or local_tool.approval_policy, arguments)
        )
        approval = self.repository.get_approval_for_execution(str(execution["id"]))
        if requires_approval and approval is None:
            approval = self.repository.create_approval_request(
                tenant_id,
                run_id,
                f"Approval required to execute tool {command.tool_name}.",
                {
                    "tool_name": command.tool_name,
                    "arguments": arguments,
                    "idempotency_key": idempotency_key,
                },
                tool_execution_id=str(execution["id"]),
            )
            self.repository.update_tool_execution(str(execution["id"]), "waiting_approval")
            self.repository.append_event(
                tenant_id,
                run_id,
                "approval_requested",
                {
                    "approval_request_id": str(approval["id"]),
                    "tool_execution_id": str(execution["id"]),
                    "tool_name": command.tool_name,
                },
            )
            self.repository.update_run_status(run_id, current_status, "waiting_approval")
            return ToolExecutionOutcome(
                status="waiting_approval",
                execution_id=str(execution["id"]),
                approval_request_id=str(approval["id"]),
            )

        status = str(execution["status"])
        if status == "completed":
            self._advance_tool_states(run_id, current_status)
            result = dict(execution.get("result") or {})
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_execution_replayed",
                {
                    "tool_execution_id": str(execution["id"]),
                    "tool_name": command.tool_name,
                    "result": result,
                },
            )
            self.repository.update_run_status(run_id, "tool_running", "queued")
            self.repository.queue_run(tenant_id, run_id)
            return ToolExecutionOutcome(
                status="completed",
                execution_id=str(execution["id"]),
                result=result,
            )

        if status == "failed":
            self._advance_tool_states(run_id, current_status)
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_execution_replayed",
                {
                    "tool_execution_id": str(execution["id"]),
                    "tool_name": command.tool_name,
                    "error": execution.get("error"),
                },
            )
            self.repository.update_run_status(run_id, "tool_running", "queued")
            self.repository.queue_run(tenant_id, run_id)
            return ToolExecutionOutcome(status="failed", execution_id=str(execution["id"]))

        if status == "running":
            self._advance_tool_states(run_id, current_status)
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_execution_in_progress",
                {
                    "tool_execution_id": str(execution["id"]),
                    "tool_name": command.tool_name,
                },
            )
            self.repository.update_run_status(run_id, "tool_running", "queued")
            self.repository.queue_run(tenant_id, run_id)
            return ToolExecutionOutcome(status="running", execution_id=str(execution["id"]))

        if approval is not None and approval.get("status") == "rejected":
            self.repository.append_event(
                tenant_id,
                run_id,
                "approval_rejected",
                {
                    "approval_request_id": str(approval["id"]),
                    "tool_execution_id": str(execution["id"]),
                },
            )
            self.repository.update_run_status(run_id, current_status, "blocked")
            return ToolExecutionOutcome(
                status="blocked",
                execution_id=str(execution["id"]),
                approval_request_id=str(approval["id"]),
            )

        if requires_approval and approval.get("status") != "approved":
            self.repository.update_run_status(run_id, current_status, "waiting_approval")
            return ToolExecutionOutcome(
                status="waiting_approval",
                execution_id=str(execution["id"]),
                approval_request_id=str(approval["id"]),
            )

        self._advance_tool_states(run_id, current_status)
        self.repository.update_tool_execution(str(execution["id"]), "running")
        self.repository.append_event(
            tenant_id,
            run_id,
            "tool_started",
            {
                "tool_execution_id": str(execution["id"]),
                "tool_name": command.tool_name,
                "arguments": arguments,
            },
        )

        context = ToolContext(
            tenant_id=tenant_id,
            run_id=run_id,
            tool_name=command.tool_name,
            execution_id=str(execution["id"]),
            idempotency_key=idempotency_key,
        )
        try:
            result = local_tool.execute(context, command.arguments)
        except Exception as exc:
            self.repository.update_tool_execution(
                str(execution["id"]),
                "failed",
                error=str(exc),
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_failed",
                {
                    "tool_execution_id": str(execution["id"]),
                    "tool_name": command.tool_name,
                    "error": str(exc),
                },
            )
            self.repository.update_run_status(run_id, "tool_running", "queued")
            self.repository.queue_run(tenant_id, run_id)
            return ToolExecutionOutcome(status="failed", execution_id=str(execution["id"]))

        event_result = {"output": result.output, "metadata": result.metadata}
        self.repository.update_tool_execution(
            str(execution["id"]),
            "completed",
            result=event_result,
        )
        self.repository.append_event(
            tenant_id,
            run_id,
            "tool_completed",
            {
                "tool_execution_id": str(execution["id"]),
                "tool_name": command.tool_name,
                "result": event_result,
            },
        )
        self.repository.update_run_status(run_id, "tool_running", "queued")
        self.repository.queue_run(tenant_id, run_id)
        return ToolExecutionOutcome(
            status="completed",
            execution_id=str(execution["id"]),
            result=event_result,
        )

    def _execute_with_database_reservation(
        self,
        run: Mapping[str, Any],
        command: ToolCommand,
        local_tool: Any,
    ) -> ToolExecutionOutcome:
        tenant_id = str(run["tenant_id"])
        run_id = str(run["id"])

        try:
            local_tool.validate_arguments(command.arguments)
        except ToolValidationError:
            self.repository.append_event(
                tenant_id,
                run_id,
                "tool_validation_failed",
                {"tool_name": command.tool_name},
            )
            raise

        arguments = redact_secrets(command.arguments)
        decision = self.repository.reserve_tool_execution(
            tenant_id,
            run_id,
            command.tool_name,
            arguments,
            force_approval=bool(local_tool.requires_approval) or _approval_policy_requires_approval(local_tool.approval_policy, arguments),
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
            )
            try:
                result = local_tool.execute(context, command.arguments)
            except Exception as exc:
                self.repository.complete_tool_execution(
                    execution_id,
                    succeeded=False,
                    error=str(exc),
                )
                return ToolExecutionOutcome(status="failed", execution_id=execution_id)

            event_result = {"output": result.output, "metadata": result.metadata}
            self.repository.complete_tool_execution(
                execution_id,
                succeeded=True,
                result=event_result,
            )
            return ToolExecutionOutcome(
                status="completed",
                execution_id=execution_id,
                result=event_result,
            )

        if decision_name == "waiting_approval":
            return ToolExecutionOutcome(
                status="waiting_approval",
                execution_id=execution_id,
                approval_request_id=approval_request_id,
            )
        if decision_name in {"replayed", "in_progress"}:
            return ToolExecutionOutcome(
                status=str(decision.get("status", "completed")),
                execution_id=execution_id,
                result=dict(decision.get("result") or {}),
            )
        if decision_name == "blocked":
            return ToolExecutionOutcome(
                status="blocked",
                execution_id=execution_id,
                approval_request_id=approval_request_id,
            )
        if decision_name == "permission_denied":
            raise ToolPermissionDenied(f"permission denied for tool: {command.tool_name}")
        if decision_name == "validation_failed":
            raise ToolValidationError(f"tool arguments failed database schema validation: {command.tool_name}")
        if decision_name in {"rejected", "missing_run"}:
            raise ToolNotRegistered(f"tool is not registered in agent_tools: {command.tool_name}")
        raise RuntimeError(f"unsupported database tool decision: {decision}")

    def _advance_tool_states(self, run_id: str, current_status: str) -> None:
        self.repository.update_run_status(run_id, current_status, "needs_tool")
        self.repository.update_run_status(run_id, "needs_tool", "tool_running")


def _approval_policy_requires_approval(policy: Any, arguments: Mapping[str, Any]) -> bool:
    if not isinstance(policy, Mapping) or not policy:
        return False
    mode = policy.get("mode")
    if mode == "always":
        return True
    if mode == "never":
        return False
    if policy.get("risk_level") in {"high", "critical"}:
        return True

    threshold = _numeric(policy.get("amount_threshold_cents"))
    if threshold is not None:
        field = str(policy.get("amount_argument", "amount_cents"))
        value = _numeric(_field_value(arguments, field))
        if value is not None and value >= threshold:
            return True

    rules = policy.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if _policy_rule_matches(rule, arguments):
                return True
    return False


def _policy_rule_matches(rule: Any, arguments: Mapping[str, Any]) -> bool:
    if not isinstance(rule, Mapping):
        return False
    if rule.get("decision", "approval_required") != "approval_required":
        return False
    field = rule.get("field")
    if not isinstance(field, str):
        return False
    value = _field_value(arguments, field)
    expected = rule.get("value")
    operator = rule.get("operator", "eq")
    if operator == "eq":
        return value == expected
    value_num = _numeric(value)
    expected_num = _numeric(expected)
    if value_num is None or expected_num is None:
        return False
    if operator in {"gte", ">="}:
        return value_num >= expected_num
    if operator in {"gt", ">"}:
        return value_num > expected_num
    if operator in {"lte", "<="}:
        return value_num <= expected_num
    if operator in {"lt", "<"}:
        return value_num < expected_num
    return False


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _field_value(payload: Mapping[str, Any], dotted_path: str) -> Any:
    value: Any = payload
    for part in dotted_path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def fail_run_for_tool_error(
    repository: ToolExecutionRepository,
    run: Mapping[str, Any],
    error: AgentError,
) -> None:
    tenant_id = str(run["tenant_id"])
    run_id = str(run["id"])
    repository.append_event(
        tenant_id,
        run_id,
        "run_failed",
        {"code": error.code, "error": str(error)},
    )
    repository.update_run_status(run_id, str(run["status"]), "failed", error=str(error))
