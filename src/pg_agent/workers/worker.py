"""Boring deterministic worker loop."""

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
    MaxIterationsExceeded,
    RunStatusConflict,
    ToolValidationError,
)
from pg_agent.runtime.final_contract import extract_answer_contract, validate_final_answer
from pg_agent.runtime.intents import (
    _intent_to_command,
    intent_to_event_payload,
    is_intent_payload,
    normalize_intent,
    parse_intent,
)
from pg_agent.runtime.prompt import build_agent_prompt
from pg_agent.tools.executor import ToolExecutor, _approval_policy_requires_approval, fail_run_for_tool_error
from pg_agent.workers.model_accounting import complete_model_call, projected_model_cost


class ModelClient(Protocol):
    def complete(self, messages: Sequence[Mapping[str, str]]) -> str | Mapping[str, Any]: ...


class WorkerRepository(Protocol):
    def load_run(self, run_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None: ...

    def load_events(self, run_id: str, *, limit: int = 200) -> list[Mapping[str, Any]]: ...

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

    def queue_run(self, tenant_id: str, run_id: str) -> None: ...

    def read_queue_message(
        self,
        *,
        visibility_timeout_seconds: int = 30,
    ) -> Mapping[str, Any] | None: ...

    def create_approval_request(
        self,
        tenant_id: str,
        run_id: str,
        reason: str,
        payload: Mapping[str, Any],
        *,
        tool_execution_id: str | None = None,
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

    def delete_queue_message(self, msg_id: int) -> None: ...


class AgentWorker:
    def __init__(
        self,
        repository: WorkerRepository,
        model_client: ModelClient,
        tool_executor: ToolExecutor,
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
        if "task_id" in payload:
            return "ignored_task_message"
        if "tenant_id" in payload and hasattr(self.repository, "set_tenant"):
            self.repository.set_tenant(str(payload["tenant_id"]))
        run_id = str(payload["run_id"])
        result = self.process_run(run_id)
        if "msg_id" in message:
            self.repository.delete_queue_message(int(message["msg_id"]))
        return result

    def process_run(self, run_id: str) -> str:
        run = self.repository.load_run(run_id, for_update=True)
        if run is None:
            return "missing"
        if str(run["status"]) != "queued":
            return "ignored"

        tenant_id = str(run["tenant_id"])
        if int(run["iteration_count"]) >= int(run["max_iterations"]):
            error = MaxIterationsExceeded(
                f"run exceeded max_iterations={run['max_iterations']}"
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "run_blocked",
                {"code": error.code, "error": str(error)},
            )
            self.repository.update_run_status(run_id, "queued", "blocked", error=str(error))
            return "blocked"

        try:
            run = self.repository.update_run_status(run_id, "queued", "thinking")
        except RunStatusConflict:
            return "ignored"
        run = self.repository.increment_iteration(run_id)
        self.repository.append_event(
            tenant_id,
            run_id,
            "run_thinking",
            {"iteration_count": run["iteration_count"]},
        )

        if hasattr(self.repository, "reserve_model_call"):
            reservation = self.repository.reserve_model_call(
                tenant_id,
                run_id,
                model=str(run.get("model", "unset")),
                actor="worker",
                projected_cost_usd=projected_model_cost(self.model_client),
            )
            if reservation.get("decision") != "allowed":
                reason = str(reservation.get("reason", "model_call_budget_exceeded"))
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "run_blocked",
                    {"reason": reason, "reservation": dict(reservation)},
                )
                self.repository.update_run_status(run_id, "thinking", "blocked", error=reason)
                return "blocked"

        events = self.repository.load_events(run_id)
        model_name = str(run.get("model", "unset"))
        model_started_at = monotonic()
        try:
            raw_command = self.model_client.complete(
                build_agent_prompt(run, events, self.tool_executor.registry.prompt_contracts())
            )
        except Exception as exc:
            complete_model_call(
                self.repository,
                tenant_id,
                run_id,
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
                {"error": str(exc)},
            )
            self.repository.update_run_status(run_id, "thinking", "failed", error=str(exc))
            return "failed"
        complete_model_call(
            self.repository,
            tenant_id,
            run_id,
            model=model_name,
            status="completed",
            latency_ms=int((monotonic() - model_started_at) * 1000),
            model_client=self.model_client,
        )

        try:
            command = parse_command(raw_command)
        except MalformedCommand as command_error:
            if not is_intent_payload(raw_command):
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "llm_command_rejected",
                    {"code": command_error.code, "error": str(command_error)},
                )
                self.repository.update_run_status(run_id, "thinking", "failed", error=str(command_error))
                return "failed"
            try:
                intent = parse_intent(raw_command)
            except MalformedCommand as intent_error:
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "llm_intent_rejected",
                    {"code": intent_error.code, "error": str(intent_error)},
                )
                self.repository.update_run_status(run_id, "thinking", "failed", error=str(intent_error))
                return "failed"

            intent_payload = intent_to_event_payload(intent)
            self.repository.append_event(
                tenant_id,
                run_id,
                "llm_intent_received",
                intent_payload,
            )
            decision = _simulate_intent_policy(
                self.repository,
                self.tool_executor,
                tenant_id,
                run_id,
                normalize_intent(intent),
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "intent_decision_recorded",
                decision,
            )
            if str(decision.get("decision")) in {"invalid", "denied", "blocked"}:
                reason = str(decision.get("reason", decision.get("decision", "intent_denied")))
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "llm_command_rejected",
                    {"code": "intent_policy_denied", "error": reason, "decision": dict(decision)},
                )
                self.repository.update_run_status(run_id, "thinking", "failed", error=reason)
                return "failed"
            command = _intent_to_command(intent)
            self.repository.append_event(
                tenant_id,
                run_id,
                "intent_mapped_to_command",
                {**intent_payload, "command_action": command.action},
            )

        self.repository.append_event(
            tenant_id,
            run_id,
            "llm_command_received",
            command_to_event_payload(command),
        )

        if isinstance(command, FinalCommand):
            contract = extract_answer_contract(run)
            validation = validate_final_answer(command.answer, contract, events)
            if not validation.valid:
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "final_answer_rejected",
                    {"errors": validation.errors, "answer": command.answer, "contract": dict(contract)},
                )
                self.repository.update_run_status(run_id, "thinking", "needs_tool")
                self.repository.update_run_status(run_id, "needs_tool", "tool_running")
                self.repository.update_run_status(run_id, "tool_running", "queued")
                self.repository.queue_run(tenant_id, run_id)
                return "queued"
            self.repository.append_event(tenant_id, run_id, "run_completed", command.answer)
            self.repository.update_run_status(
                run_id,
                "thinking",
                "completed",
                answer=command.answer,
            )
            return "completed"

        if isinstance(command, FailCommand):
            self.repository.append_event(
                tenant_id,
                run_id,
                "run_failed",
                {"reason": command.reason},
            )
            self.repository.update_run_status(
                run_id,
                "thinking",
                "failed",
                error=command.reason,
            )
            return "failed"

        if isinstance(command, AskHumanCommand):
            approval = self.repository.create_approval_request(
                tenant_id,
                run_id,
                command.reason,
                command.payload,
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "approval_requested",
                {"approval_request_id": str(approval["id"]), "payload": command.payload},
            )
            self.repository.update_run_status(run_id, "thinking", "waiting_approval")
            return "waiting_approval"

        if isinstance(command, RememberCommand):
            self.repository.update_run_status(run_id, "thinking", "needs_tool")
            self.repository.update_run_status(run_id, "needs_tool", "tool_running")
            memory = self.repository.create_memory(
                tenant_id,
                command.memory_type,
                command.content,
                command.metadata,
                source_run_id=run_id,
            )
            self.repository.append_event(
                tenant_id,
                run_id,
                "memory_recorded",
                {"memory_id": str(memory["id"]), "memory_type": command.memory_type},
            )
            self.repository.update_run_status(run_id, "tool_running", "queued")
            self.repository.queue_run(tenant_id, run_id)
            return "queued"

        if isinstance(command, ToolCommand):
            try:
                outcome = self.tool_executor.execute(run, command)
            except ToolValidationError as exc:
                self.repository.append_event(
                    tenant_id,
                    run_id,
                    "tool_command_correction_requested",
                    {"code": exc.code, "error": str(exc), "tool_name": command.tool_name},
                )
                self.repository.update_run_status(run_id, "thinking", "needs_tool")
                self.repository.update_run_status(run_id, "needs_tool", "tool_running")
                self.repository.update_run_status(run_id, "tool_running", "queued")
                self.repository.queue_run(tenant_id, run_id)
                return "queued"
            except AgentError as exc:
                fail_run_for_tool_error(self.repository, run, exc)
                return "failed"
            return outcome.status

        if isinstance(command, DelegateCommand):
            self.repository.append_event(
                tenant_id,
                run_id,
                "run_failed",
                {"reason": "delegate commands require AgentTaskWorker"},
            )
            self.repository.update_run_status(
                run_id,
                "thinking",
                "failed",
                error="delegate commands require AgentTaskWorker",
            )
            return "failed"

        raise AssertionError(f"unhandled command type: {type(command)!r}")


def _simulate_intent_policy(
    repository: WorkerRepository,
    tool_executor: ToolExecutor,
    tenant_id: str,
    run_id: str,
    intent: Mapping[str, Any],
) -> Mapping[str, Any]:
    simulator = getattr(repository, "simulate_agent_intent_policy", None)
    if callable(simulator):
        decision = dict(simulator(tenant_id, run_id, intent, actor="worker"))
    else:
        intent_name = str(intent.get("intent", ""))
        if intent_name in {"final_answer", "failure"}:
            decision = {"decision": "terminal", "status": "allowed", "intent": intent_name}
        else:
            decision = {"decision": "allowed", "status": "allowed", "intent": intent_name}

    if str(intent.get("intent")) == "tool_request" and decision.get("decision") == "allowed":
        try:
            local_tool = tool_executor.registry.get(str(intent.get("tool_name", "")))
        except AgentError as exc:
            decision["decision"] = "invalid"
            decision["status"] = "failed"
            decision["reason"] = str(exc)
            return decision
        arguments = intent.get("arguments") if isinstance(intent.get("arguments"), Mapping) else {}
        if local_tool.requires_approval or _approval_policy_requires_approval(local_tool.approval_policy, arguments):
            decision["decision"] = "requires_approval"
            decision["status"] = "waiting_approval"
            decision["reason"] = "local_tool_policy_requires_approval"
    return decision
