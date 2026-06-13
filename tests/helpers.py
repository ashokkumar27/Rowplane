from __future__ import annotations

import itertools
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rowplane.runtime.errors import ApprovalAlreadyResolved, RunStatusConflict, ToolValidationError
from rowplane.runtime.sanitize import stable_hash
from rowplane.runtime.schema import validate_json_schema_subset
from rowplane.runtime.states import validate_transition
from rowplane.runtime.task_states import validate_task_transition
from rowplane.tools.executor import _approval_policy_requires_approval


class FakeRepository:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.task_dependencies: dict[str, dict[str, Any]] = {}
        self.runtime_budgets: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.tools: dict[tuple[str, str], dict[str, Any]] = {}
        self.permissions: dict[tuple[str, str, str, str], bool] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.memories: dict[str, dict[str, Any]] = {}
        self.eval_results: dict[str, dict[str, Any]] = {}
        self.work_leases: dict[str, dict[str, Any]] = {}
        self.queued: list[tuple[str, str]] = []
        self.queued_tasks: list[tuple[str, str, str]] = []
        self.queue_messages: list[dict[str, Any]] = []
        self.deleted_messages: list[int] = []
        self.tenant_context: str | None = None
        self._ids = itertools.count(1)

    def next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    def set_tenant(self, tenant_id: str) -> None:
        self.tenant_context = tenant_id

    def add_queue_message(
        self,
        tenant_id: str,
        run_id: str,
        *,
        msg_id: int = 1,
        task_id: str | None = None,
    ) -> None:
        payload = {"tenant_id": tenant_id, "run_id": run_id}
        if task_id is not None:
            payload["task_id"] = task_id
        self.queue_messages.append({"msg_id": msg_id, "message": payload})

    def read_queue_message(self, *, visibility_timeout_seconds: int = 30) -> Mapping[str, Any] | None:
        if not self.queue_messages:
            return None
        return self.queue_messages.pop(0)

    def add_run(
        self,
        run_id: str = "run_1",
        tenant_id: str = "tenant_1",
        status: str = "queued",
        iteration_count: int = 0,
        max_iterations: int = 5,
        required_capabilities: Sequence[str] | None = None,
        priority: int = 0,
        not_before: Any = None,
        deadline_at: Any = None,
    ) -> dict[str, Any]:
        run = {
            "id": run_id,
            "tenant_id": tenant_id,
            "status": status,
            "task": {"input": "test"},
            "answer": None,
            "error": None,
            "iteration_count": iteration_count,
            "max_iterations": max_iterations,
            "model": "test-model",
            "required_capabilities": list(required_capabilities or []),
            "priority": priority,
            "not_before": not_before,
            "deadline_at": deadline_at,
        }
        self.runs[run_id] = run
        return run

    def add_agent(
        self,
        name: str,
        *,
        agent_id: str | None = None,
        tenant_id: str = "tenant_1",
        role: str = "specialist",
        instructions: str = "Do the assigned task.",
        enabled: bool = True,
    ) -> dict[str, Any]:
        agent = {
            "id": agent_id or self.next_id("agent"),
            "tenant_id": tenant_id,
            "name": name,
            "role": role,
            "instructions": instructions,
            "model": "test-model",
            "enabled": enabled,
        }
        self.agents[agent["id"]] = agent
        return agent

    def add_task(
        self,
        agent_id: str,
        *,
        task_id: str = "task_1",
        tenant_id: str = "tenant_1",
        run_id: str = "run_1",
        parent_task_id: str | None = None,
        status: str = "queued",
        input: Mapping[str, Any] | None = None,
        iteration_count: int = 0,
        max_iterations: int = 5,
        required_capabilities: Sequence[str] | None = None,
        priority: int = 0,
        not_before: Any = None,
        deadline_at: Any = None,
    ) -> dict[str, Any]:
        task = {
            "id": task_id,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "agent_id": agent_id,
            "parent_task_id": parent_task_id,
            "status": status,
            "input": dict(input or {"input": "test"}),
            "output": None,
            "error": None,
            "iteration_count": iteration_count,
            "max_iterations": max_iterations,
            "required_capabilities": list(required_capabilities or []),
            "priority": priority,
            "not_before": not_before,
            "deadline_at": deadline_at,
        }
        self.tasks[task_id] = task
        return task

    def create_agent_task(
        self,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        task_input: Mapping[str, Any],
        *,
        parent_task_id: str | None = None,
        max_iterations: int = 10,
        required_capabilities: Sequence[str] | None = None,
        priority: int = 0,
        not_before: Any = None,
        deadline_at: Any = None,
    ) -> Mapping[str, Any]:
        return self.add_task(
            agent_id,
            task_id=self.next_id("task"),
            tenant_id=tenant_id,
            run_id=run_id,
            parent_task_id=parent_task_id,
            input=task_input,
            max_iterations=max_iterations,
            required_capabilities=required_capabilities,
            priority=priority,
            not_before=not_before,
            deadline_at=deadline_at,
        )

    def load_task(self, task_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None:
        return self.tasks.get(task_id)

    def load_agent(self, agent_id: str) -> Mapping[str, Any] | None:
        return self.agents.get(agent_id)

    def get_agent_by_name(self, tenant_id: str, name: str) -> Mapping[str, Any] | None:
        for agent in self.agents.values():
            if agent["tenant_id"] == tenant_id and agent["name"] == name and agent.get("enabled", True):
                return agent
        return None

    def update_task_status(
        self,
        task_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        task = self.tasks[task_id]
        if task["status"] != current_status:
            raise RunStatusConflict(
                f"task {task_id} was {task['status']} not {current_status}"
            )
        validate_task_transition(current_status, next_status)
        old_status = task["status"]
        task["status"] = next_status
        task.update(fields)
        if old_status != next_status:
            self.append_event(
                task["tenant_id"],
                task["run_id"],
                "task_status_changed",
                {
                    "task_id": task_id,
                    "agent_id": task["agent_id"],
                    "from": old_status,
                    "to": next_status,
                },
                actor="db",
            )
        return task

    def increment_task_iteration(self, task_id: str) -> Mapping[str, Any]:
        self.tasks[task_id]["iteration_count"] += 1
        return self.tasks[task_id]

    def create_agent_message(
        self,
        tenant_id: str,
        run_id: str,
        message_type: str,
        content: Mapping[str, Any],
        *,
        from_task_id: str | None = None,
        to_task_id: str | None = None,
    ) -> Mapping[str, Any]:
        message = {
            "id": self.next_id("message"),
            "tenant_id": tenant_id,
            "run_id": run_id,
            "from_task_id": from_task_id,
            "to_task_id": to_task_id,
            "message_type": message_type,
            "content": dict(content),
        }
        self.messages.append(message)
        return message

    def add_runtime_budget(
        self,
        tenant_id: str = "tenant_1",
        *,
        scope_type: str = "tenant",
        scope_id: str | None = None,
        max_child_tasks: int | None = None,
        max_active_work: int | None = None,
        max_model_calls: int | None = None,
        max_tool_executions: int | None = None,
        max_estimated_cost_usd: float | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        budget = {
            "id": self.next_id("budget"),
            "tenant_id": tenant_id,
            "scope_type": scope_type,
            "scope_id": scope_id or tenant_id,
            "max_child_tasks": max_child_tasks,
            "max_active_work": max_active_work,
            "max_model_calls": max_model_calls,
            "max_tool_executions": max_tool_executions,
            "max_estimated_cost_usd": max_estimated_cost_usd,
            "enabled": enabled,
        }
        self.runtime_budgets.append(budget)
        return budget

    def reserve_model_call(
        self,
        tenant_id: str,
        run_id: str,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        model: str = "unset",
        actor: str = "worker",
        projected_cost_usd: float | None = None,
    ) -> Mapping[str, Any]:
        budget = self.runtime_budget_allows(
            tenant_id,
            "model_calls",
            increment=1,
            run_id=run_id,
            task_id=task_id,
            agent_id=agent_id,
            actor=actor,
        )
        if not bool(budget.get("allowed", True)):
            self.append_event(
                tenant_id,
                run_id,
                "model_call_denied_by_budget",
                {"task_id": task_id, "agent_id": agent_id, "model": model, "budget": dict(budget)},
                actor=actor,
            )
            return {"decision": "denied", "status": "blocked", "reason": "model_call_budget_exceeded", "budget": budget}

        cost_budget = self.runtime_cost_budget_allows(
            tenant_id,
            projected_cost_usd=projected_cost_usd or 0,
            run_id=run_id,
            task_id=task_id,
            agent_id=agent_id,
            actor=actor,
        )
        if not bool(cost_budget.get("allowed", True)):
            self.append_event(
                tenant_id,
                run_id,
                "model_call_denied_by_budget",
                {"task_id": task_id, "agent_id": agent_id, "model": model, "budget": dict(cost_budget)},
                actor=actor,
            )
            return {"decision": "denied", "status": "blocked", "reason": "model_cost_budget_exceeded", "budget": cost_budget}

        self.append_event(
            tenant_id,
            run_id,
            "model_call_reserved",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "model": model,
                "projected_cost_usd": projected_cost_usd or 0,
                "budget": dict(budget),
                "cost_budget": dict(cost_budget),
            },
            actor=actor,
        )
        return {
            "decision": "allowed",
            "status": "reserved",
            "model": model,
            "task_id": task_id,
            "agent_id": agent_id,
            "projected_cost_usd": projected_cost_usd or 0,
            "budget": budget,
            "cost_budget": cost_budget,
        }

    def complete_model_call(
        self,
        tenant_id: str,
        run_id: str,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        model: str = "unset",
        status: str = "completed",
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
        error: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        event_type = "model_call_failed" if status == "failed" else "model_call_completed"
        self.append_event(
            tenant_id,
            run_id,
            event_type,
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "model": model,
                "status": status,
                "latency_ms": latency_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "error": error,
            },
            actor=actor,
        )
        return {"decision": "recorded", "status": status, "event_type": event_type}

    def runtime_cost_budget_allows(
        self,
        tenant_id: str,
        *,
        projected_cost_usd: float = 0,
        run_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        scopes = [("tenant", tenant_id)]
        if run_id is not None:
            scopes.append(("run", run_id))
        if task_id is not None:
            scopes.append(("task", task_id))
        if agent_id is not None:
            scopes.append(("agent", agent_id))
        for scope_type, scope_id in scopes:
            for budget in self.runtime_budgets:
                if not budget.get("enabled", True):
                    continue
                if budget["tenant_id"] != tenant_id or budget["scope_type"] != scope_type or budget["scope_id"] != scope_id:
                    continue
                limit = budget.get("max_estimated_cost_usd")
                if limit is None:
                    continue
                usage = self._runtime_cost_budget_usage(tenant_id, scope_type, scope_id)
                if usage + projected_cost_usd > float(limit):
                    if run_id is not None:
                        self.append_event(
                            tenant_id,
                            run_id,
                            "runtime_budget_exceeded",
                            {
                                "budget_id": budget["id"],
                                "scope_type": scope_type,
                                "scope_id": scope_id,
                                "metric": "estimated_cost_usd",
                                "usage": usage,
                                "projected_cost_usd": projected_cost_usd,
                                "limit": limit,
                                "task_id": task_id,
                                "agent_id": agent_id,
                            },
                            actor=actor,
                        )
                    return {
                        "allowed": False,
                        "decision": "denied",
                        "metric": "estimated_cost_usd",
                        "budget_id": budget["id"],
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "usage": usage,
                        "projected_cost_usd": projected_cost_usd,
                        "limit": limit,
                    }
        if run_id is not None and any(
            budget.get("enabled", True)
            and budget["tenant_id"] == tenant_id
            and budget.get("max_estimated_cost_usd") is not None
            for budget in self.runtime_budgets
        ):
            self.append_event(
                tenant_id,
                run_id,
                "runtime_budget_checked",
                {"metric": "estimated_cost_usd", "allowed": True, "task_id": task_id, "agent_id": agent_id},
                actor=actor,
            )
        return {"allowed": True, "decision": "allowed", "metric": "estimated_cost_usd"}

    def runtime_budget_allows(
        self,
        tenant_id: str,
        metric: str,
        *,
        increment: int = 1,
        run_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        scopes = [("tenant", tenant_id)]
        if run_id is not None:
            scopes.append(("run", run_id))
        if task_id is not None:
            scopes.append(("task", task_id))
        if agent_id is not None:
            scopes.append(("agent", agent_id))
        for scope_type, scope_id in scopes:
            for budget in self.runtime_budgets:
                if not budget.get("enabled", True):
                    continue
                if budget["tenant_id"] != tenant_id or budget["scope_type"] != scope_type or budget["scope_id"] != scope_id:
                    continue
                limit = budget.get(f"max_{metric}")
                if limit is None:
                    continue
                usage = self._runtime_budget_usage(tenant_id, scope_type, scope_id, metric)
                if usage + increment > limit:
                    if run_id is not None:
                        self.append_event(
                            tenant_id,
                            run_id,
                            "runtime_budget_exceeded",
                            {
                                "budget_id": budget["id"],
                                "scope_type": scope_type,
                                "scope_id": scope_id,
                                "metric": metric,
                                "usage": usage,
                                "increment": increment,
                                "limit": limit,
                                "task_id": task_id,
                                "agent_id": agent_id,
                            },
                            actor=actor,
                        )
                    return {
                        "allowed": False,
                        "decision": "denied",
                        "metric": metric,
                        "budget_id": budget["id"],
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "usage": usage,
                        "increment": increment,
                        "limit": limit,
                    }
        if run_id is not None:
            self.append_event(tenant_id, run_id, "runtime_budget_checked", {"metric": metric, "allowed": True, "task_id": task_id, "agent_id": agent_id}, actor=actor)
        return {"allowed": True, "decision": "allowed", "metric": metric}

    def _runtime_budget_usage(self, tenant_id: str, scope_type: str, scope_id: str, metric: str) -> int:
        if metric == "child_tasks":
            if scope_type == "tenant":
                return sum(1 for task in self.tasks.values() if task["tenant_id"] == tenant_id)
            if scope_type == "run":
                return sum(1 for task in self.tasks.values() if task["tenant_id"] == tenant_id and task["run_id"] == scope_id)
            if scope_type == "task":
                return sum(1 for task in self.tasks.values() if task["tenant_id"] == tenant_id and task.get("parent_task_id") == scope_id)
            if scope_type == "agent":
                return sum(1 for task in self.tasks.values() if task["tenant_id"] == tenant_id and task.get("agent_id") == scope_id)
        if metric == "active_work":
            if scope_type == "tenant":
                return sum(1 for lease in self.work_leases.values() if lease["tenant_id"] == tenant_id and lease["status"] == "active")
            if scope_type == "run":
                return sum(1 for lease in self.work_leases.values() if lease["tenant_id"] == tenant_id and lease["run_id"] == scope_id and lease["status"] == "active")
            if scope_type == "task":
                return sum(1 for lease in self.work_leases.values() if lease["tenant_id"] == tenant_id and lease.get("task_id") == scope_id and lease["status"] == "active")
        if metric == "model_calls":
            reserved_events = [
                event
                for event in self.events
                if event["tenant_id"] == tenant_id and event["event_type"] == "model_call_reserved"
            ]
            if scope_type == "tenant":
                return len(reserved_events)
            if scope_type == "run":
                return sum(1 for event in reserved_events if event["run_id"] == scope_id)
            if scope_type == "task":
                return sum(1 for event in reserved_events if event["payload"].get("task_id") == scope_id)
            if scope_type == "agent":
                return sum(1 for event in reserved_events if event["payload"].get("agent_id") == scope_id)
        return 0

    def _runtime_cost_budget_usage(self, tenant_id: str, scope_type: str, scope_id: str) -> float:
        completed_events = [
            event
            for event in self.events
            if event["tenant_id"] == tenant_id and event["event_type"] in {"model_call_completed", "model_call_failed"}
        ]
        if scope_type == "tenant":
            scoped = completed_events
        elif scope_type == "run":
            scoped = [event for event in completed_events if event["run_id"] == scope_id]
        elif scope_type == "task":
            scoped = [event for event in completed_events if event["payload"].get("task_id") == scope_id]
        elif scope_type == "agent":
            scoped = [event for event in completed_events if event["payload"].get("agent_id") == scope_id]
        else:
            scoped = []
        return sum(float(event["payload"].get("estimated_cost_usd") or 0) for event in scoped)

    def create_task_dependency(
        self,
        tenant_id: str,
        run_id: str,
        parent_task_id: str,
        child_task_id: str,
        *,
        dependency_type: str = "completion",
        required: bool = True,
        metadata: Mapping[str, Any] | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        for dependency in self.task_dependencies.values():
            if (
                dependency["tenant_id"] == tenant_id
                and dependency["parent_task_id"] == parent_task_id
                and dependency["child_task_id"] == child_task_id
                and dependency["dependency_type"] == dependency_type
            ):
                dependency["required"] = required
                dependency["metadata"].update(dict(metadata or {}))
                row = dependency
                break
        else:
            row = {
                "id": self.next_id("dependency"),
                "tenant_id": tenant_id,
                "run_id": run_id,
                "parent_task_id": parent_task_id,
                "child_task_id": child_task_id,
                "dependency_type": dependency_type,
                "required": required,
                "status": "waiting",
                "metadata": dict(metadata or {}),
            }
            self.task_dependencies[row["id"]] = row
        self.append_event(
            tenant_id,
            run_id,
            "task_dependency_created",
            {
                "dependency_id": row["id"],
                "parent_task_id": parent_task_id,
                "child_task_id": child_task_id,
                "dependency_type": dependency_type,
                "required": required,
            },
            actor=actor,
        )
        return {
            "decision": "created",
            "dependency_id": row["id"],
            "parent_task_id": parent_task_id,
            "child_task_id": child_task_id,
            "status": row["status"],
        }

    def complete_task_dependencies_for_child(
        self,
        tenant_id: str,
        run_id: str,
        child_task_id: str,
        child_status: str,
        *,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        dependency_status = "satisfied" if child_status == "completed" else "failed"
        updated = [
            dependency
            for dependency in self.task_dependencies.values()
            if dependency["tenant_id"] == tenant_id
            and dependency["run_id"] == run_id
            and dependency["child_task_id"] == child_task_id
            and dependency["status"] == "waiting"
        ]
        for dependency in updated:
            dependency["status"] = dependency_status
            dependency["metadata"]["child_status"] = child_status
            self.append_event(
                tenant_id,
                run_id,
                "task_dependency_satisfied" if dependency_status == "satisfied" else "task_dependency_failed",
                {
                    "dependency_id": dependency["id"],
                    "parent_task_id": dependency["parent_task_id"],
                    "child_task_id": child_task_id,
                    "child_status": child_status,
                    "required": dependency["required"],
                },
                actor=actor,
            )

        released_count = 0
        blocked_count = 0
        parent_ids = {dependency["parent_task_id"] for dependency in self.task_dependencies.values() if dependency["tenant_id"] == tenant_id and dependency["run_id"] == run_id and dependency["child_task_id"] == child_task_id}
        for parent_task_id in parent_ids:
            parent = self.tasks.get(parent_task_id)
            if parent is None:
                continue
            required_dependencies = [
                dependency
                for dependency in self.task_dependencies.values()
                if dependency["tenant_id"] == tenant_id
                and dependency["run_id"] == run_id
                and dependency["parent_task_id"] == parent_task_id
                and dependency["required"]
            ]
            if any(dependency["status"] == "failed" for dependency in required_dependencies):
                if parent["status"] == "waiting_child":
                    self.update_task_status(parent_task_id, "waiting_child", "blocked", error="required child task dependency failed")
                    blocked_count += 1
                    self.append_event(tenant_id, run_id, "task_dependency_parent_blocked", {"parent_task_id": parent_task_id, "child_task_id": child_task_id}, actor=actor)
            elif required_dependencies and all(dependency["status"] == "satisfied" for dependency in required_dependencies):
                if parent["status"] == "waiting_child":
                    self.update_task_status(parent_task_id, "waiting_child", "queued")
                    self.queue_task(tenant_id, run_id, parent_task_id)
                    released_count += 1
                    self.append_event(tenant_id, run_id, "task_dependency_parent_released", {"parent_task_id": parent_task_id, "child_task_id": child_task_id}, actor=actor)
        return {
            "decision": "no_dependencies" if not updated else "updated",
            "updated_count": len(updated),
            "released_count": released_count,
            "blocked_count": blocked_count,
            "child_task_id": child_task_id,
            "child_status": child_status,
        }

    def load_task_messages(
        self,
        run_id: str,
        task_id: str,
        *,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]:
        rows = [
            message
            for message in self.messages
            if message["run_id"] == run_id
            and (
                message["to_task_id"] == task_id
                or message["from_task_id"] == task_id
                or message["to_task_id"] is None
            )
        ]
        return rows[-limit:]

    def add_tool(
        self,
        tenant_id: str,
        name: str,
        *,
        tool_id: str | None = None,
        enabled: bool = True,
        requires_approval: bool = False,
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        approval_policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        tool = {
            "id": tool_id or self.next_id("tool"),
            "tenant_id": tenant_id,
            "name": name,
            "enabled": enabled,
            "requires_approval": requires_approval,
            "input_schema": dict(input_schema or {"type": "object"}),
            "output_schema": dict(output_schema or {"type": "object"}),
            "approval_policy": dict(approval_policy or {}),
        }
        self.tools[(tenant_id, name)] = tool
        return tool

    def grant_tenant(self, tenant_id: str, tool_id: str) -> None:
        self.permissions[(tenant_id, tool_id, "tenant", tenant_id)] = True

    def grant_agent(self, tenant_id: str, tool_id: str, agent_id: str) -> None:
        self.permissions[(tenant_id, tool_id, "agent", agent_id)] = True

    def deny_run(self, tenant_id: str, tool_id: str, run_id: str) -> None:
        self.permissions[(tenant_id, tool_id, "run", run_id)] = False

    def load_run(self, run_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None:
        return self.runs.get(run_id)

    def load_events(self, run_id: str, *, limit: int = 200) -> list[Mapping[str, Any]]:
        return [event for event in self.events if event["run_id"] == run_id][-limit:]

    def append_event(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "worker",
    ) -> None:
        self.events.append(
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "event_type": event_type,
                "payload": dict(payload),
                "actor": actor,
            }
        )

    def update_run_status(
        self,
        run_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        run = self.runs[run_id]
        if run["status"] != current_status:
            raise RunStatusConflict(
                f"run {run_id} was {run['status']} not {current_status}"
            )
        validate_transition(current_status, next_status)
        old_status = run["status"]
        run["status"] = next_status
        run.update(fields)
        if old_status != next_status:
            self.append_event(
                run["tenant_id"],
                run_id,
                "run_status_changed",
                {"from": old_status, "to": next_status},
                actor="db",
            )
        return run

    def increment_iteration(self, run_id: str) -> Mapping[str, Any]:
        self.runs[run_id]["iteration_count"] += 1
        return self.runs[run_id]

    def queue_run(self, tenant_id: str, run_id: str) -> None:
        self.queued.append((tenant_id, run_id))

    def queue_task(self, tenant_id: str, run_id: str, task_id: str) -> None:
        self.queued_tasks.append((tenant_id, run_id, task_id))

    def claim_agent_work(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        capabilities: Sequence[str] | None = None,
        max_items: int = 1,
        lease_seconds: int = 60,
        kinds: Sequence[str] | None = None,
        actor: str = "scheduler",
    ) -> list[Mapping[str, Any]]:
        selected: list[tuple[str, dict[str, Any]]] = []
        allowed = list(kinds or ["task", "run"])
        if "task" in allowed:
            for task in sorted(self.tasks.values(), key=lambda item: (-int(item.get("priority", 0)), str(item.get("deadline_at") or "~"), str(item.get("id")))):
                run = self.runs.get(str(task["run_id"]))
                if task["tenant_id"] != tenant_id or task["status"] != "queued":
                    continue
                if not set(task.get("required_capabilities", [])) <= set(capabilities or []):
                    continue
                if task.get("not_before") is not None:
                    continue
                if run is None or run["status"] in {"completed", "failed", "blocked"}:
                    continue
                if self._has_active_work_lease(tenant_id, task_id=str(task["id"])):
                    continue
                selected.append(("task", task))
                if len(selected) >= max_items:
                    break
        if "run" in allowed and len(selected) < max_items:
            for run in sorted(self.runs.values(), key=lambda item: (-int(item.get("priority", 0)), str(item.get("deadline_at") or "~"), str(item.get("id")))):
                if run["tenant_id"] != tenant_id or run["status"] != "queued":
                    continue
                if not set(run.get("required_capabilities", [])) <= set(capabilities or []):
                    continue
                if run.get("not_before") is not None:
                    continue
                if self._has_active_work_lease(tenant_id, run_id=str(run["id"])):
                    continue
                if any(
                    task["tenant_id"] == tenant_id
                    and task["run_id"] == run["id"]
                    and task["status"] == "queued"
                    for task in self.tasks.values()
                ):
                    continue
                selected.append(("run", run))
                if len(selected) >= max_items:
                    break

        claims: list[Mapping[str, Any]] = []
        for work_type, row in selected:
            lease = {
                "id": self.next_id("lease"),
                "tenant_id": tenant_id,
                "run_id": row["run_id"] if work_type == "task" else row["id"],
                "task_id": row["id"] if work_type == "task" else None,
                "work_type": work_type,
                "worker_id": worker_id,
                "capabilities": list(capabilities or []),
                "status": "active",
                "metadata": {},
            }
            self.work_leases[lease["id"]] = lease
            self.append_event(
                tenant_id,
                str(lease["run_id"]),
                "work_claimed",
                {
                    "work_lease_id": lease["id"],
                    "task_id": lease["task_id"],
                    "work_type": work_type,
                    "worker_id": worker_id,
                },
                actor=actor,
            )
            claims.append(
                {
                    "work_lease_id": lease["id"],
                    "tenant_id": tenant_id,
                    "run_id": lease["run_id"],
                    "task_id": lease["task_id"],
                    "work_type": work_type,
                    "lease_expires_at": None,
                    "payload": {},
                }
            )
        return claims

    def complete_agent_work(
        self,
        work_lease_id: str,
        worker_id: str,
        *,
        status: str = "completed",
        metadata: Mapping[str, Any] | None = None,
        actor: str = "scheduler",
    ) -> Mapping[str, Any]:
        lease = self.work_leases.get(work_lease_id)
        if lease is None or lease["worker_id"] != worker_id or lease["status"] != "active":
            return {"decision": "not_active", "status": "failed"}
        lease["status"] = status
        lease["metadata"].update(dict(metadata or {}))
        self.append_event(
            str(lease["tenant_id"]),
            str(lease["run_id"]),
            "work_lease_completed",
            {
                "work_lease_id": work_lease_id,
                "task_id": lease.get("task_id"),
                "work_type": lease["work_type"],
                "worker_id": worker_id,
                "status": status,
                "metadata": dict(metadata or {}),
            },
            actor=actor,
        )
        return {"decision": "completed", "status": status, "work_lease_id": work_lease_id}

    def heartbeat_agent_work(
        self,
        work_lease_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        actor: str = "scheduler",
    ) -> Mapping[str, Any]:
        lease = self.work_leases.get(work_lease_id)
        if lease is None or lease["worker_id"] != worker_id or lease["status"] != "active":
            return {"decision": "not_active", "status": "failed"}
        self.append_event(
            str(lease["tenant_id"]),
            str(lease["run_id"]),
            "work_heartbeat",
            {"work_lease_id": work_lease_id, "worker_id": worker_id},
            actor=actor,
        )
        return {"decision": "extended", "status": "active", "work_lease_id": work_lease_id}

    def _has_active_work_lease(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> bool:
        for lease in self.work_leases.values():
            if lease["tenant_id"] != tenant_id or lease["status"] != "active":
                continue
            if task_id is not None and lease.get("task_id") == task_id:
                return True
            if task_id is None and run_id is not None and lease.get("task_id") is None and lease.get("run_id") == run_id:
                return True
        return False

    def delete_queue_message(self, msg_id: int) -> None:
        self.deleted_messages.append(msg_id)

    def get_agent_tool(self, tenant_id: str, tool_name: str) -> Mapping[str, Any] | None:
        return self.tools.get((tenant_id, tool_name))

    def has_tool_permission(
        self,
        tenant_id: str,
        tool_id: str,
        run_id: str,
        *,
        agent_id: str | None = None,
    ) -> bool:
        run_key = (tenant_id, tool_id, "run", run_id)
        if run_key in self.permissions:
            return self.permissions[run_key]
        if agent_id is not None:
            agent_key = (tenant_id, tool_id, "agent", agent_id)
            if agent_key in self.permissions:
                return self.permissions[agent_key]
        tenant_key = (tenant_id, tool_id, "tenant", tenant_id)
        return bool(self.permissions.get(tenant_key, False))

    def simulate_agent_intent_policy(
        self,
        tenant_id: str,
        run_id: str,
        intent: Mapping[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        intent_name = str(intent.get("intent", ""))
        run = self.runs.get(run_id)
        if run is None:
            return {"decision": "blocked", "status": "failed", "reason": "run_not_found"}
        if run["status"] in {"completed", "failed"}:
            return {"decision": "terminal", "status": run["status"]}
        if run["status"] == "blocked":
            return {"decision": "blocked", "status": "blocked"}
        if intent_name in {"final_answer", "failure"}:
            return {"decision": "terminal", "status": "allowed", "intent": intent_name}
        if intent_name in {"clarification_request", "memory_proposal", "delegation_request"}:
            return {"decision": "allowed", "status": "allowed", "intent": intent_name}
        if intent_name != "tool_request":
            return {"decision": "invalid", "status": "failed", "reason": "unsupported_intent"}

        tool_name = str(intent.get("tool_name", ""))
        arguments = intent.get("arguments") if isinstance(intent.get("arguments"), Mapping) else {}
        db_tool = self.get_agent_tool(tenant_id, tool_name)
        if db_tool is None or not db_tool.get("enabled", False):
            return {"decision": "denied", "status": "failed", "reason": "not_registered_or_disabled"}
        tool_id = str(db_tool["id"])
        if not self.has_tool_permission(tenant_id, tool_id, run_id, agent_id=agent_id):
            return {"decision": "denied", "status": "failed", "reason": "permission_denied", "tool_id": tool_id}
        try:
            validate_json_schema_subset(db_tool.get("input_schema"), arguments, subject="tool.arguments")
        except ToolValidationError:
            return {"decision": "invalid", "status": "failed", "reason": "tool_schema_validation_failed", "tool_id": tool_id}

        arguments_hash = stable_hash(dict(arguments))
        idempotency_key = stable_hash(
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "tool_name": tool_name,
                "arguments_hash": arguments_hash,
            }
        )
        execution = self.get_tool_execution_by_key(tenant_id, tool_id, idempotency_key)
        if execution is not None:
            approval = self.get_approval_for_execution(str(execution["id"]))
            if approval is not None and approval.get("status") == "rejected":
                return {"decision": "blocked", "status": "blocked", "reason": "approval_rejected", "tool_execution_id": execution["id"], "approval_request_id": approval["id"]}
            if execution.get("status") in {"completed", "failed", "running"}:
                return {"decision": "idempotent_replay", "status": execution["status"], "tool_execution_id": execution["id"], "idempotency_key": idempotency_key}
            if execution.get("status") == "waiting_approval" or (approval is not None and approval.get("status") == "pending"):
                return {"decision": "requires_approval", "status": "waiting_approval", "tool_execution_id": execution["id"], "approval_request_id": approval["id"] if approval else None, "idempotency_key": idempotency_key}

        requires_approval = bool(db_tool.get("requires_approval")) or _approval_policy_requires_approval(db_tool.get("approval_policy"), arguments)
        if requires_approval:
            return {"decision": "requires_approval", "status": "waiting_approval", "tool_id": tool_id, "idempotency_key": idempotency_key}
        return {"decision": "allowed", "status": "running", "tool_id": tool_id, "idempotency_key": idempotency_key}

    def get_tool_execution_by_key(
        self,
        tenant_id: str,
        tool_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        for execution in self.executions.values():
            if (
                execution["tenant_id"] == tenant_id
                and execution["tool_id"] == tool_id
                and execution["idempotency_key"] == idempotency_key
            ):
                return execution
        return None

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
    ) -> Mapping[str, Any]:
        existing = self.get_tool_execution_by_key(tenant_id, tool_id, idempotency_key)
        if existing is not None:
            return existing
        execution = {
            "id": self.next_id("exec"),
            "tenant_id": tenant_id,
            "run_id": run_id,
            "task_id": task_id,
            "tool_id": tool_id,
            "idempotency_key": idempotency_key,
            "arguments": dict(arguments),
            "arguments_hash": arguments_hash,
            "status": "pending",
            "result": None,
            "error": None,
        }
        self.executions[execution["id"]] = execution
        return execution

    def update_tool_execution(
        self,
        execution_id: str,
        status: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        execution = self.executions[execution_id]
        execution["status"] = status
        execution.update(fields)
        return execution

    def create_approval_request(
        self,
        tenant_id: str,
        run_id: str,
        reason: str,
        payload: Mapping[str, Any],
        *,
        tool_execution_id: str | None = None,
        task_id: str | None = None,
    ) -> Mapping[str, Any]:
        if tool_execution_id is not None:
            existing = self.get_approval_for_execution(tool_execution_id)
            if existing is not None:
                return existing
        approval = {
            "id": self.next_id("approval"),
            "tenant_id": tenant_id,
            "run_id": run_id,
            "task_id": task_id,
            "tool_execution_id": tool_execution_id,
            "reason": reason,
            "payload": dict(payload),
            "status": "pending",
            "resolved_by": None,
        }
        self.approvals[approval["id"]] = approval
        return approval

    def get_approval_for_execution(self, execution_id: str) -> Mapping[str, Any] | None:
        for approval in reversed(list(self.approvals.values())):
            if approval["tool_execution_id"] == execution_id:
                return approval
        return None

    def get_approval_request(self, approval_id: str) -> Mapping[str, Any] | None:
        return self.approvals.get(approval_id)

    def resolve_approval_request(
        self,
        approval_id: str,
        status: str,
        resolved_by: str,
    ) -> Mapping[str, Any]:
        approval = self.approvals[approval_id]
        if approval["status"] != "pending":
            raise ApprovalAlreadyResolved(f"approval request is already {approval['status']}")
        approval["status"] = status
        approval["resolved_by"] = resolved_by
        return approval

    def create_memory(
        self,
        tenant_id: str,
        memory_type: str,
        content: str,
        metadata: Mapping[str, Any],
        *,
        source_run_id: str | None = None,
        embedding: Any = None,
    ) -> Mapping[str, Any]:
        memory = {
            "id": self.next_id("memory"),
            "tenant_id": tenant_id,
            "memory_type": memory_type,
            "content": content,
            "metadata": dict(metadata),
            "source_run_id": source_run_id,
            "embedding": embedding,
        }
        self.memories[memory["id"]] = memory
        return memory


    def list_memory_for_run(self, run_id: str, *, limit: int = 100) -> list[Mapping[str, Any]]:
        return [memory for memory in self.memories.values() if memory.get("source_run_id") == run_id][:limit]

    def search_memory(self, search: Any) -> list[Mapping[str, Any]]:
        rows = [memory for memory in self.memories.values() if memory["tenant_id"] == search.tenant_id]
        if search.memory_type is not None:
            rows = [memory for memory in rows if memory["memory_type"] == search.memory_type]
        if search.source_run_id is not None:
            rows = [memory for memory in rows if memory.get("source_run_id") == search.source_run_id]
        if search.metadata_contains:
            rows = [
                memory for memory in rows
                if all(memory.get("metadata", {}).get(key) == value for key, value in search.metadata_contains.items())
            ]
        if search.query:
            needle = search.query.lower()
            rows = [memory for memory in rows if needle in (memory["memory_type"] + " " + memory["content"] + " " + str(memory["metadata"])).lower()]
        return rows[:search.limit]

    def list_run_trajectory(self, tenant_id: str, run_id: str, *, limit: int = 500) -> list[Mapping[str, Any]]:
        rows = [
            {
                "source": "event",
                "sequence_id": index + 1,
                "created_at": None,
                "step_type": event["event_type"],
                "actor": event.get("actor", "worker"),
                "payload": event.get("payload", {}),
            }
            for index, event in enumerate(self.events)
            if event["tenant_id"] == tenant_id and event["run_id"] == run_id
        ]
        return rows[:limit]

    def create_eval_result(
        self,
        tenant_id: str,
        eval_case_id: str,
        run_id: str,
        scores: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        result = {
            "id": self.next_id("eval"),
            "tenant_id": tenant_id,
            "eval_case_id": eval_case_id,
            "run_id": run_id,
            "scores": dict(scores),
        }
        self.eval_results[result["id"]] = result
        return result


class StaticModel:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.messages: list[Any] = []

    def complete(self, messages: Any) -> Any:
        self.messages.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response
