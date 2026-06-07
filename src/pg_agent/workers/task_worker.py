"""Deterministic worker loop for multi-agent tasks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from time import monotonic
from typing import Any, Protocol

from pg_agent.runtime.commands import (
    AskHumanCommand,
    DelegateCommand,
    FailCommand,
    FinalCommand,
    RememberCommand,
    ToolCommand,
    command_to_event_payload,
    parse_command,
)
from pg_agent.runtime.errors import (
    AgentError,
    MalformedCommand,
    RunStatusConflict,
)
from pg_agent.runtime.final_contract import extract_answer_contract, extract_answer_contract_from_payload, validate_final_answer
from pg_agent.runtime.prompt import build_agent_task_prompt
from pg_agent.runtime.states import TERMINAL_STATUSES
from pg_agent.tools.task_executor import TaskToolExecutor
from pg_agent.workers.model_accounting import complete_model_call, projected_model_cost


class ModelClient(Protocol):
    def complete(self, messages: Sequence[Mapping[str, str]]) -> str | Mapping[str, Any]: ...


class TaskWorkerRepository(Protocol):
    def load_run(self, run_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None: ...

    def load_task(self, task_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None: ...

    def load_agent(self, agent_id: str) -> Mapping[str, Any] | None: ...

    def get_agent_by_name(self, tenant_id: str, name: str) -> Mapping[str, Any] | None: ...

    def load_events(self, run_id: str, *, limit: int = 200) -> list[Mapping[str, Any]]: ...

    def load_task_messages(
        self,
        run_id: str,
        task_id: str,
        *,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]: ...

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

    def increment_iteration(self, run_id: str) -> Mapping[str, Any]: ...

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
    ) -> Mapping[str, Any]: ...

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
    ) -> Mapping[str, Any]: ...

    def update_task_status(
        self,
        task_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]: ...

    def increment_task_iteration(self, task_id: str) -> Mapping[str, Any]: ...

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
    ) -> Mapping[str, Any]: ...

    def create_agent_message(
        self,
        tenant_id: str,
        run_id: str,
        message_type: str,
        content: Mapping[str, Any],
        *,
        from_task_id: str | None = None,
        to_task_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def queue_task(self, tenant_id: str, run_id: str, task_id: str) -> None: ...

    def read_queue_message(
        self,
        *,
        visibility_timeout_seconds: int = 30,
    ) -> Mapping[str, Any] | None: ...

    def delete_queue_message(self, msg_id: int) -> None: ...

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

    def create_memory(
        self,
        tenant_id: str,
        memory_type: str,
        content: str,
        metadata: Mapping[str, Any],
        *,
        source_run_id: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> Mapping[str, Any]: ...


class AgentTaskWorker:
    """Interpret one queued task row at a time."""

    def __init__(
        self,
        repository: TaskWorkerRepository,
        model_client: ModelClient,
        tool_executor: TaskToolExecutor,
    ) -> None:
        self.repository = repository
        self.model_client = model_client
        self.tool_executor = tool_executor

    def run_once(self, *, visibility_timeout_seconds: int = 30) -> str:
        message = self.repository.read_queue_message(
            visibility_timeout_seconds=visibility_timeout_seconds
        )
        if message is None:
            return "empty"
        return self.process_message(message)

    def process_message(self, message: Mapping[str, Any]) -> str:
        payload = message.get("message", message)
        if "task_id" not in payload:
            return "ignored_run_message"
        if "tenant_id" in payload and hasattr(self.repository, "set_tenant"):
            self.repository.set_tenant(str(payload["tenant_id"]))
        result = self.process_task(str(payload["task_id"]))
        if "msg_id" in message:
            self.repository.delete_queue_message(int(message["msg_id"]))
        return result

    def process_task(self, task_id: str) -> str:
        task = self.repository.load_task(task_id, for_update=True)
        if task is None:
            return "missing"
        if str(task["status"]) != "queued":
            return "ignored"

        run_id = str(task["run_id"])
        run = self.repository.load_run(run_id, for_update=True)
        if run is None:
            return "missing_run"
        if str(run["status"]) in {str(status) for status in TERMINAL_STATUSES}:
            return "ignored"

        tenant_id = str(task["tenant_id"])
        agent_id = str(task["agent_id"])
        agent = self.repository.load_agent(agent_id)
        if agent is None:
            return self._block_task_and_maybe_run(
                run,
                task,
                f"agent not found for task: {agent_id}",
            )

        if self._is_root_task(task) and str(run["status"]) == "queued":
            run = self.repository.update_run_status(run_id, "queued", "thinking")

        if self._run_or_task_exceeded_budget(run, task):
            return self._block_task_and_maybe_run(
                run,
                task,
                (
                    f"task exceeded max_iterations={task['max_iterations']} "
                    f"or run exceeded max_iterations={run['max_iterations']}"
                ),
            )

        try:
            task = self.repository.update_task_status(task_id, "queued", "thinking")
        except RunStatusConflict:
            return "ignored"
        task = self.repository.increment_task_iteration(task_id)
        run = self.repository.increment_iteration(run_id)
        self.repository.append_event(
            tenant_id,
            run_id,
            "task_thinking",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "iteration_count": task["iteration_count"],
                "run_iteration_count": run["iteration_count"],
            },
        )

        if hasattr(self.repository, "reserve_model_call"):
            reservation = self.repository.reserve_model_call(
                tenant_id,
                run_id,
                task_id=task_id,
                agent_id=agent_id,
                model=str(agent.get("model", run.get("model", "unset"))),
                actor="worker",
                projected_cost_usd=projected_model_cost(self.model_client),
            )
            if reservation.get("decision") != "allowed":
                reason = str(reservation.get("reason", "model_call_budget_exceeded"))
                self._block_task_and_maybe_run(run, task, reason)
                return "blocked"

        events = self.repository.load_events(run_id)
        messages = self.repository.load_task_messages(run_id, task_id)
        model_name = str(agent.get("model", run.get("model", "unset")))
        model_started_at = monotonic()
        try:
            raw_command = self.model_client.complete(
                build_agent_task_prompt(run, task, agent, events, messages)
            )
        except Exception as exc:
            complete_model_call(
                self.repository,
                tenant_id,
                run_id,
                task_id=task_id,
                agent_id=agent_id,
                model=model_name,
                status="failed",
                latency_ms=int((monotonic() - model_started_at) * 1000),
                model_client=self.model_client,
                error=str(exc),
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "llm_call_failed",
                {"task_id": task_id, "agent_id": agent_id, "error": str(exc)},
            )
            self._fail_task(run, task, agent, str(exc), code="llm_call_failed")
            return "failed"
        complete_model_call(
            self.repository,
            tenant_id,
            run_id,
            task_id=task_id,
            agent_id=agent_id,
            model=model_name,
            status="completed",
            latency_ms=int((monotonic() - model_started_at) * 1000),
            model_client=self.model_client,
        )

        try:
            command = parse_command(raw_command)
        except MalformedCommand as exc:
            self.repository.append_event(
                tenant_id,
                run_id,
                "llm_command_rejected",
                {"task_id": task_id, "agent_id": agent_id, "code": exc.code, "error": str(exc)},
            )
            self._fail_task(run, task, agent, str(exc), code=exc.code)
            return "failed"

        command_payload = command_to_event_payload(command)
        command_payload.update({"task_id": task_id, "agent_id": agent_id})
        self.repository.append_event(
            tenant_id,
            run_id,
            "llm_command_received",
            command_payload,
        )

        if isinstance(command, DelegateCommand):
            return self._handle_delegate(run, task, agent, command)

        if isinstance(command, FinalCommand):
            return self._handle_final(run, task, agent, command)

        if isinstance(command, FailCommand):
            self.repository.append_event(
                tenant_id,
                run_id,
                "task_failed",
                {"task_id": task_id, "agent_id": agent_id, "reason": command.reason},
            )
            self._fail_task(run, task, agent, command.reason)
            return "failed"

        if isinstance(command, AskHumanCommand):
            approval = self.repository.create_approval_request(
                tenant_id,
                run_id,
                command.reason,
                command.payload,
                task_id=task_id,
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "approval_requested",
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "approval_request_id": str(approval["id"]),
                    "payload": command.payload,
                },
            )
            self.repository.update_task_status(task_id, "thinking", "waiting_approval")
            return "waiting_approval"

        if isinstance(command, RememberCommand):
            self.repository.update_task_status(task_id, "thinking", "needs_tool")
            self.repository.update_task_status(task_id, "needs_tool", "tool_running")
            metadata = dict(command.metadata)
            metadata.setdefault("task_id", task_id)
            metadata.setdefault("agent_id", agent_id)
            memory = self.repository.create_memory(
                tenant_id,
                command.memory_type,
                command.content,
                metadata,
                source_run_id=run_id,
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "memory_recorded",
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "memory_id": str(memory["id"]),
                    "memory_type": command.memory_type,
                },
            )
            self.repository.update_task_status(task_id, "tool_running", "queued")
            self.repository.queue_task(tenant_id, run_id, task_id)
            return "queued"

        if isinstance(command, ToolCommand):
            try:
                outcome = self.tool_executor.execute(run, task, agent, command)
            except AgentError as exc:
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "task_failed",
                    {"task_id": task_id, "agent_id": agent_id, "code": exc.code, "error": str(exc)},
                )
                self._fail_task(run, task, agent, str(exc), code=exc.code)
                return "failed"
            if outcome.status == "blocked":
                blocked_task = self.repository.load_task(task_id) or task
                self._notify_parent_of_terminal_task(run, blocked_task, agent, "blocked")
            return outcome.status

        raise AssertionError(f"unhandled command type: {type(command)!r}")

    def _handle_delegate(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        agent: Mapping[str, Any],
        command: DelegateCommand,
    ) -> str:
        tenant_id = str(run["tenant_id"])
        run_id = str(run["id"])
        task_id = str(task["id"])
        agent_id = str(agent["id"])
        child_agent = self.repository.get_agent_by_name(tenant_id, command.to_agent)
        if child_agent is None:
            self.repository.append_event(
                tenant_id,
                run_id,
                "delegation_failed",
                {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "to_agent": command.to_agent,
                    "reason": "agent_not_found_or_disabled",
                },
            )
            self._fail_task(run, task, agent, f"delegate target not found: {command.to_agent}")
            return "failed"

        if hasattr(self.repository, "runtime_budget_allows"):
            budget = self.repository.runtime_budget_allows(
                tenant_id,
                "child_tasks",
                increment=1,
                run_id=run_id,
                task_id=task_id,
                agent_id=agent_id,
                actor="worker",
            )
            if not bool(budget.get("allowed", False)):
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "delegation_rejected_by_budget",
                    {
                        "task_id": task_id,
                        "agent_id": agent_id,
                        "to_agent": command.to_agent,
                        "budget": dict(budget),
                    },
                )
                self._block_task_and_maybe_run(run, task, f"delegation rejected by budget: {budget.get('metric', 'child_tasks')}")
                return "blocked"

        child_required_capabilities = _string_list(command.task.get("required_capabilities"))
        if not child_required_capabilities:
            child_required_capabilities = [f"agent:{command.to_agent}"]
        child = self.repository.create_agent_task(
            tenant_id,
            run_id,
            str(child_agent["id"]),
            command.task,
            parent_task_id=task_id,
            max_iterations=min(int(task.get("max_iterations", 10)), 8),
            required_capabilities=child_required_capabilities,
            priority=int(command.task.get("priority", task.get("priority", 0)) or 0),
            not_before=command.task.get("not_before"),
            deadline_at=command.task.get("deadline_at"),
        )
        self.repository.create_agent_message(
            tenant_id,
            run_id,
            "delegation",
            {
                "from_agent": agent.get("name"),
                "to_agent": command.to_agent,
                "reason": command.reason,
                "task": command.task,
            },
            from_task_id=task_id,
            to_task_id=str(child["id"]),
        )
        if hasattr(self.repository, "create_task_dependency"):
            self.repository.create_task_dependency(
                tenant_id,
                run_id,
                task_id,
                str(child["id"]),
                dependency_type="completion",
                required=True,
                metadata={"to_agent": command.to_agent, "reason": command.reason},
                actor="worker",
            )
        self.repository.append_event(
            tenant_id,
            run_id,
            "delegation_created",
            {
                "parent_task_id": task_id,
                "child_task_id": str(child["id"]),
                "from_agent_id": agent_id,
                "to_agent_id": str(child_agent["id"]),
                "to_agent": command.to_agent,
                "reason": command.reason,
            },
        )
        self.repository.update_task_status(task_id, "thinking", "waiting_child")
        self.repository.queue_task(tenant_id, run_id, str(child["id"]))
        return "waiting_child"

    def _handle_final(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        agent: Mapping[str, Any],
        command: FinalCommand,
    ) -> str:
        tenant_id = str(run["tenant_id"])
        run_id = str(run["id"])
        task_id = str(task["id"])
        agent_id = str(agent["id"])
        contract = extract_answer_contract_from_payload(task.get("input") if isinstance(task.get("input"), Mapping) else {})
        if not contract and self._is_root_task(task):
            contract = extract_answer_contract(run)
        validation = validate_final_answer(command.answer, contract, self.repository.load_events(run_id, limit=500))
        if not validation.valid:
            self.repository.append_event(
                tenant_id,
                run_id,
                "final_answer_rejected",
                {"task_id": task_id, "agent_id": agent_id, "errors": validation.errors, "answer": command.answer, "contract": dict(contract)},
            )
            self.repository.update_task_status(task_id, "thinking", "needs_tool")
            self.repository.update_task_status(task_id, "needs_tool", "tool_running")
            self.repository.update_task_status(task_id, "tool_running", "queued")
            self.repository.queue_task(tenant_id, run_id, task_id)
            return "queued"

        completed_task = self.repository.update_task_status(
            task_id,
            "thinking",
            "completed",
            output=command.answer,
        )
        self.repository.append_event(
            tenant_id,
            run_id,
            "task_completed",
            {"task_id": task_id, "agent_id": agent_id, "answer": command.answer},
        )
        if not self._is_root_task(task):
            self._notify_parent_of_terminal_task(run, completed_task, agent, "completed")
            return "completed"

        self.repository.append_event(tenant_id, run_id, "run_completed", command.answer)
        latest_run = self.repository.load_run(run_id, for_update=True) or run
        self.repository.update_run_status(
            run_id,
            str(latest_run["status"]),
            "completed",
            answer=command.answer,
        )
        return "completed"

    def _fail_task(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        agent: Mapping[str, Any],
        reason: str,
        *,
        code: str | None = None,
    ) -> Mapping[str, Any]:
        task_id = str(task["id"])
        failed_task = self.repository.update_task_status(
            task_id,
            str(task["status"]),
            "failed",
            error=reason,
        )
        if self._is_root_task(task):
            run_id = str(run["id"])
            tenant_id = str(run["tenant_id"])
            payload: dict[str, Any] = {"reason": reason}
            if code is not None:
                payload["code"] = code
            self.repository.append_event(tenant_id, run_id, "run_failed", payload)
            latest_run = self.repository.load_run(run_id, for_update=True) or run
            self.repository.update_run_status(
                run_id,
                str(latest_run["status"]),
                "failed",
                error=reason,
            )
        else:
            self._notify_parent_of_terminal_task(run, failed_task, agent, "failed")
        return failed_task

    def _block_task_and_maybe_run(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        reason: str,
    ) -> str:
        tenant_id = str(task["tenant_id"])
        run_id = str(task["run_id"])
        task_id = str(task["id"])
        self.repository.append_event(
            tenant_id,
            run_id,
            "task_blocked",
            {"task_id": task_id, "agent_id": str(task["agent_id"]), "error": reason},
        )
        blocked_task = self.repository.update_task_status(
            task_id,
            str(task["status"]),
            "blocked",
            error=reason,
        )
        agent = self.repository.load_agent(str(task["agent_id"])) or {"id": task["agent_id"]}
        if self._is_root_task(task):
            self.repository.append_event(
                tenant_id,
                run_id,
                "run_blocked",
                {"task_id": task_id, "error": reason},
            )
            latest_run = self.repository.load_run(run_id, for_update=True) or run
            self.repository.update_run_status(
                run_id,
                str(latest_run["status"]),
                "blocked",
                error=reason,
            )
        else:
            self._notify_parent_of_terminal_task(run, blocked_task, agent, "blocked")
        return "blocked"

    def _notify_parent_of_terminal_task(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
        agent: Mapping[str, Any],
        status: str,
    ) -> None:
        parent_task_id = task.get("parent_task_id")
        if not parent_task_id:
            return

        tenant_id = str(run["tenant_id"])
        run_id = str(run["id"])
        task_id = str(task["id"])
        content = {
            "child_task_id": task_id,
            "agent_id": str(agent.get("id", task.get("agent_id"))),
            "agent_name": agent.get("name"),
            "status": status,
            "output": task.get("output"),
            "error": task.get("error"),
        }
        self.repository.create_agent_message(
            tenant_id,
            run_id,
            "task_result",
            content,
            from_task_id=task_id,
            to_task_id=str(parent_task_id),
        )
        self.repository.append_event(
            tenant_id,
            run_id,
            "task_result_reported",
            {
                "parent_task_id": str(parent_task_id),
                "child_task_id": task_id,
                "status": status,
            },
        )
        if hasattr(self.repository, "complete_task_dependencies_for_child"):
            dependency_result = self.repository.complete_task_dependencies_for_child(
                tenant_id,
                run_id,
                task_id,
                status,
                actor="worker",
            )
            if dependency_result.get("decision") != "no_dependencies":
                return

        parent = self.repository.load_task(str(parent_task_id), for_update=True)
        if parent is None:
            return
        if str(parent["status"]) == "waiting_child":
            self.repository.update_task_status(str(parent_task_id), "waiting_child", "queued")
            self.repository.queue_task(tenant_id, run_id, str(parent_task_id))

    def _run_or_task_exceeded_budget(
        self,
        run: Mapping[str, Any],
        task: Mapping[str, Any],
    ) -> bool:
        return (
            int(task["iteration_count"]) >= int(task["max_iterations"])
            or int(run["iteration_count"]) >= int(run["max_iterations"])
        )

    def _is_root_task(self, task: Mapping[str, Any]) -> bool:
        return task.get("parent_task_id") is None



def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]
