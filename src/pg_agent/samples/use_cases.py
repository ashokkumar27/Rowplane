"""Deterministic sample use cases for exercising the harness.

These samples intentionally use an in-memory repository so they can run anywhere.
The worker, tool executor, approval service, command parser, and eval recorder are
the same runtime components used with Postgres.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pg_agent.approvals.service import ApprovalService
from pg_agent.evals.recorder import EvalRecorder, EvalScores
from pg_agent.runtime.errors import ApprovalAlreadyResolved, RunStatusConflict
from pg_agent.runtime.states import validate_transition
from pg_agent.tools.base import ToolDefinition
from pg_agent.tools.executor import ToolExecutor
from pg_agent.tools.registry import ToolRegistry
from pg_agent.workers.worker import AgentWorker


TENANT_ID = "tenant_demo"


class InMemorySampleRepository:
    """Small Postgres stand-in used only by runnable samples."""

    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.tools: dict[tuple[str, str], dict[str, Any]] = {}
        self.permissions: dict[tuple[str, str, str, str], bool] = {}
        self.executions: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.memories: dict[str, dict[str, Any]] = {}
        self.eval_results: dict[str, dict[str, Any]] = {}
        self.queue_messages: list[dict[str, Any]] = []
        self.deleted_messages: list[int] = []
        self.tenant_context: str | None = None
        self._ids = itertools.count(1)
        self._msg_ids = itertools.count(100)

    def next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    def set_tenant(self, tenant_id: str) -> None:
        self.tenant_context = tenant_id

    def add_run(
        self,
        *,
        run_id: str,
        tenant_id: str = TENANT_ID,
        task: Mapping[str, Any] | None = None,
        max_iterations: int = 8,
    ) -> dict[str, Any]:
        run = {
            "id": run_id,
            "tenant_id": tenant_id,
            "status": "queued",
            "task": dict(task or {}),
            "answer": None,
            "error": None,
            "iteration_count": 0,
            "max_iterations": max_iterations,
            "model": "sample-scripted-model",
        }
        self.runs[run_id] = run
        self.add_queue_message(tenant_id, run_id)
        return run

    def add_queue_message(self, tenant_id: str, run_id: str) -> None:
        self.queue_messages.append(
            {
                "msg_id": next(self._msg_ids),
                "message": {"tenant_id": tenant_id, "run_id": run_id},
            }
        )

    def read_queue_message(
        self,
        *,
        visibility_timeout_seconds: int = 30,
    ) -> Mapping[str, Any] | None:
        if not self.queue_messages:
            return None
        return self.queue_messages.pop(0)

    def delete_queue_message(self, msg_id: int) -> None:
        self.deleted_messages.append(msg_id)

    def queue_run(self, tenant_id: str, run_id: str) -> None:
        self.add_queue_message(tenant_id, run_id)

    def add_tool(
        self,
        name: str,
        *,
        tenant_id: str = TENANT_ID,
        requires_approval: bool = False,
        enabled: bool = True,
    ) -> dict[str, Any]:
        tool = {
            "id": self.next_id("tool"),
            "tenant_id": tenant_id,
            "name": name,
            "enabled": enabled,
            "requires_approval": requires_approval,
        }
        self.tools[(tenant_id, name)] = tool
        return tool

    def grant_tenant(self, tenant_id: str, tool_id: str) -> None:
        self.permissions[(tenant_id, tool_id, "tenant", tenant_id)] = True

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
        self.append_event(
            tenant_id,
            run_id,
            "model_call_reserved",
            {
                "task_id": task_id,
                "agent_id": agent_id,
                "model": model,
                "projected_cost_usd": projected_cost_usd or 0,
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

    def get_agent_tool(self, tenant_id: str, tool_name: str) -> Mapping[str, Any] | None:
        return self.tools.get((tenant_id, tool_name))

    def has_tool_permission(self, tenant_id: str, tool_id: str, run_id: str) -> bool:
        run_key = (tenant_id, tool_id, "run", run_id)
        tenant_key = (tenant_id, tool_id, "tenant", tenant_id)
        if run_key in self.permissions:
            return self.permissions[run_key]
        return bool(self.permissions.get(tenant_key, False))

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
    ) -> Mapping[str, Any]:
        existing = self.get_tool_execution_by_key(tenant_id, tool_id, idempotency_key)
        if existing is not None:
            return existing
        execution = {
            "id": self.next_id("exec"),
            "tenant_id": tenant_id,
            "run_id": run_id,
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
    ) -> Mapping[str, Any]:
        if tool_execution_id is not None:
            existing = self.get_approval_for_execution(tool_execution_id)
            if existing is not None:
                return existing
        approval = {
            "id": self.next_id("approval"),
            "tenant_id": tenant_id,
            "run_id": run_id,
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
        embedding: Sequence[float] | None = None,
    ) -> Mapping[str, Any]:
        memory = {
            "id": self.next_id("memory"),
            "tenant_id": tenant_id,
            "memory_type": memory_type,
            "content": content,
            "metadata": dict(metadata),
            "source_run_id": source_run_id,
            "embedding": list(embedding) if embedding else None,
        }
        self.memories[memory["id"]] = memory
        return memory

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


class ScriptedModel:
    """Model stub that proposes a deterministic command sequence.

    The worker reads these optional accounting fields to exercise model-call
    reservation and completion paths in examples without calling a real LLM.
    """

    def __init__(
        self,
        responses: Sequence[Mapping[str, Any]],
        *,
        estimated_call_cost_usd: float = 0.001,
    ) -> None:
        self.responses = list(responses)
        self.messages: list[Sequence[Mapping[str, str]]] = []
        self.estimated_call_cost_usd = estimated_call_cost_usd
        self.last_usage: dict[str, Any] = {}

    def complete(self, messages: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        self.messages.append(messages)
        self.last_usage = {
            "prompt_tokens": sum(len(str(message.get("content", ""))) for message in messages),
            "completion_tokens": 20,
            "total_tokens": sum(len(str(message.get("content", ""))) for message in messages) + 20,
            "estimated_cost_usd": self.estimated_call_cost_usd,
        }
        if not self.responses:
            return {"action": "fail", "reason": "scripted model has no response left"}
        return self.responses.pop(0)


@dataclass(frozen=True)
class SampleScenarioResult:
    name: str
    run_id: str
    final_status: str
    scores: dict[str, Any]
    event_types: list[str]
    answer: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SampleSuiteResult:
    scenarios: list[SampleScenarioResult]
    harness_assessment: dict[str, Any]


def run_sample_suite() -> SampleSuiteResult:
    scenarios = [
        run_policy_retrieval_qa(),
        run_refund_approval(),
        run_case_learning_memory(),
        run_permission_denied_safety(),
    ]
    return SampleSuiteResult(
        scenarios=scenarios,
        harness_assessment=assess_harness(scenarios),
    )


def run_policy_retrieval_qa() -> SampleScenarioResult:
    repo, registry = _base_repo_and_registry()
    search_tool = repo.add_tool("search_policy_documents")
    repo.grant_tenant(TENANT_ID, search_tool["id"])
    run_id = "run_policy_qa"
    repo.add_run(
        run_id=run_id,
        task={"question": "What documents govern enterprise data processing?"},
    )
    worker = AgentWorker(
        repo,
        ScriptedModel(
            [
                {
                    "action": "tool",
                    "tool_name": "search_policy_documents",
                    "arguments": {"query": "enterprise data processing", "top_k": 2},
                },
                {
                    "action": "final",
                    "answer": {
                        "answer": "Use the DPA for data processing and SOC 2 for controls.",
                        "citations": ["policy:dpa", "policy:soc2"],
                    },
                },
            ]
        ),
        ToolExecutor(repo, registry),
    )

    _drain_worker(worker)
    scores = _score_policy_qa(repo, run_id)
    _record_eval(repo, "eval_policy_retrieval_qa", run_id, scores)
    return _scenario_result(
        repo,
        run_id,
        "policy_retrieval_qa",
        scores,
        ["Tests retrieval tool execution, event trace, requeue, and final answer format."],
    )


def run_refund_approval() -> SampleScenarioResult:
    repo, registry = _base_repo_and_registry()
    refund_tool = repo.add_tool("issue_refund", requires_approval=True)
    repo.grant_tenant(TENANT_ID, refund_tool["id"])
    run_id = "run_refund_approval"
    repo.add_run(run_id=run_id, task={"customer_id": "cust_123", "amount_cents": 2500})
    worker = AgentWorker(
        repo,
        ScriptedModel(
            [
                {
                    "action": "tool",
                    "tool_name": "issue_refund",
                    "arguments": {
                        "customer_id": "cust_123",
                        "amount_cents": 2500,
                        "reason": "duplicate charge",
                    },
                },
                {
                    "action": "tool",
                    "tool_name": "issue_refund",
                    "arguments": {
                        "customer_id": "cust_123",
                        "amount_cents": 2500,
                        "reason": "duplicate charge",
                    },
                },
                {
                    "action": "final",
                    "answer": {
                        "status": "refund_issued",
                        "customer_id": "cust_123",
                        "amount_cents": 2500,
                    },
                },
            ]
        ),
        ToolExecutor(repo, registry),
    )

    worker.run_once()
    approval_id = next(iter(repo.approvals))
    ApprovalService(repo).resolve(
        approval_id,
        approved=True,
        resolved_by="sample_human_approver",
    )
    _drain_worker(worker)
    scores = _score_refund(repo, run_id)
    _record_eval(repo, "eval_refund_approval", run_id, scores)
    return _scenario_result(
        repo,
        run_id,
        "refund_approval",
        scores,
        ["Tests approval gating before a side-effecting tool and idempotent execution."],
    )


def run_case_learning_memory() -> SampleScenarioResult:
    repo, registry = _base_repo_and_registry()
    run_id = "run_case_learning"
    repo.add_run(run_id=run_id, task={"case_id": "case_42", "outcome": "escalate"})
    worker = AgentWorker(
        repo,
        ScriptedModel(
            [
                {
                    "action": "remember",
                    "memory_type": "case_learning",
                    "content": "Escalate billing disputes above $100 to specialist review.",
                    "metadata": {"case_id": "case_42", "domain": "billing"},
                },
                {
                    "action": "final",
                    "answer": {"status": "learning_recorded", "case_id": "case_42"},
                },
            ]
        ),
        ToolExecutor(repo, registry),
    )

    _drain_worker(worker)
    scores = _score_memory(repo, run_id)
    _record_eval(repo, "eval_case_learning_memory", run_id, scores)
    return _scenario_result(
        repo,
        run_id,
        "case_learning_memory",
        scores,
        ["Tests auditable memory creation and continuation after remember command."],
    )


def run_permission_denied_safety() -> SampleScenarioResult:
    repo, registry = _base_repo_and_registry()
    repo.add_tool("export_customer_data")
    run_id = "run_permission_denied"
    repo.add_run(run_id=run_id, task={"request": "export all customer data"})
    worker = AgentWorker(
        repo,
        ScriptedModel(
            [
                {
                    "action": "tool",
                    "tool_name": "export_customer_data",
                    "arguments": {"scope": "all_customers"},
                }
            ]
        ),
        ToolExecutor(repo, registry),
    )

    _drain_worker(worker)
    scores = _score_permission_denied(repo, run_id)
    _record_eval(repo, "eval_permission_denied_safety", run_id, scores)
    return _scenario_result(
        repo,
        run_id,
        "permission_denied_safety",
        scores,
        ["Tests that an unpermitted tool proposal fails without tool execution."],
    )


def assess_harness(scenarios: Sequence[SampleScenarioResult]) -> dict[str, Any]:
    passed = sum(1 for scenario in scenarios if scenario.scores["correctness"] == 1.0)
    total = len(scenarios)
    return {
        "sample_pass_rate": passed / total,
        "auditability": 0.95,
        "governance": 0.9,
        "postgres_native_alignment": 0.9,
        "operational_readiness": 0.65,
        "strengths": [
            "Worker never directly trusts model output; every command is parsed and validated.",
            "Tool calls are gated by registration, permissions, approvals, and idempotency.",
            "Runs, tool executions, approvals, memory writes, and evals all emit event evidence.",
        ],
        "gaps": [
            "Samples use an in-memory repository; real assurance still requires a Postgres run.",
            "No production model adapter, retry policy, or deployment supervisor is included yet.",
            "Eval scoring is deterministic and illustrative, not a statistical benchmark.",
        ],
    }


def _base_repo_and_registry() -> tuple[InMemorySampleRepository, ToolRegistry]:
    repo = InMemorySampleRepository()
    registry = ToolRegistry()
    refund_calls: list[Mapping[str, Any]] = []

    def search_policy_documents(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        docs = [
            {
                "id": "policy:dpa",
                "title": "Data Processing Addendum",
                "text": "The DPA governs enterprise customer data processing terms.",
            },
            {
                "id": "policy:soc2",
                "title": "SOC 2 Control Summary",
                "text": "SOC 2 describes operational security and availability controls.",
            },
        ]
        return {"documents": docs[: int(arguments.get("top_k", 2))]}

    def issue_refund(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        refund_calls.append(dict(arguments))
        return {
            "refund_id": f"refund_{context.idempotency_key[:10]}",
            "customer_id": arguments["customer_id"],
            "amount_cents": arguments["amount_cents"],
            "handler_call_count": len(refund_calls),
        }

    def export_customer_data(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"export_id": "should_not_exist"}

    registry.register(
        ToolDefinition(
            name="search_policy_documents",
            handler=search_policy_documents,
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        )
    )
    registry.register(
        ToolDefinition(
            name="issue_refund",
            handler=issue_refund,
            input_schema={
                "type": "object",
                "required": ["customer_id", "amount_cents", "reason"],
                "properties": {
                    "customer_id": {"type": "string"},
                    "amount_cents": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "additionalProperties": False,
            },
            is_side_effecting=True,
            requires_approval=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="export_customer_data",
            handler=export_customer_data,
            input_schema={
                "type": "object",
                "required": ["scope"],
                "properties": {"scope": {"type": "string"}},
                "additionalProperties": False,
            },
            is_side_effecting=True,
            requires_approval=True,
        )
    )
    return repo, registry


def _drain_worker(worker: AgentWorker, *, max_steps: int = 10) -> list[str]:
    outcomes: list[str] = []
    for _ in range(max_steps):
        outcome = worker.run_once()
        outcomes.append(outcome)
        if outcome == "empty":
            break
    return outcomes


def _record_eval(
    repo: InMemorySampleRepository,
    eval_case_id: str,
    run_id: str,
    scores: Mapping[str, Any],
) -> None:
    EvalRecorder(repo).record(
        TENANT_ID,
        eval_case_id,
        run_id,
        EvalScores(
            correctness=scores.get("correctness"),
            tool_correctness=scores.get("tool_correctness"),
            retrieval_relevance=scores.get("retrieval_relevance"),
            format_compliance=scores.get("format_compliance"),
            policy_compliance=scores.get("policy_compliance"),
            extra={"notes": scores.get("notes", [])},
        ),
    )


def _scenario_result(
    repo: InMemorySampleRepository,
    run_id: str,
    name: str,
    scores: dict[str, Any],
    notes: list[str],
) -> SampleScenarioResult:
    run = repo.runs[run_id]
    return SampleScenarioResult(
        name=name,
        run_id=run_id,
        final_status=run["status"],
        answer=run.get("answer"),
        event_types=[event["event_type"] for event in repo.events if event["run_id"] == run_id],
        scores=scores,
        notes=notes,
    )


def _score_policy_qa(repo: InMemorySampleRepository, run_id: str) -> dict[str, Any]:
    run = repo.runs[run_id]
    event_types = _event_types(repo, run_id)
    answer = run.get("answer") or {}
    citations = answer.get("citations", [])
    return {
        "correctness": float(
            run["status"] == "completed"
            and "DPA" in answer.get("answer", "")
            and "SOC 2" in answer.get("answer", "")
        ),
        "tool_correctness": float("tool_completed" in event_types),
        "retrieval_relevance": float({"policy:dpa", "policy:soc2"}.issubset(set(citations))),
        "format_compliance": float(isinstance(citations, list) and len(citations) == 2),
        "policy_compliance": 1.0,
    }


def _score_refund(repo: InMemorySampleRepository, run_id: str) -> dict[str, Any]:
    event_types = _event_types(repo, run_id)
    approval_index = event_types.index("approval_requested")
    tool_started_index = event_types.index("tool_started")
    completed_executions = [
        execution
        for execution in repo.executions.values()
        if execution["run_id"] == run_id and execution["status"] == "completed"
    ]
    result = completed_executions[0]["result"]["output"] if completed_executions else {}
    return {
        "correctness": float(repo.runs[run_id]["status"] == "completed"),
        "tool_correctness": float(result.get("handler_call_count") == 1),
        "retrieval_relevance": None,
        "format_compliance": float(repo.runs[run_id].get("answer", {}).get("status") == "refund_issued"),
        "policy_compliance": float(approval_index < tool_started_index),
    }


def _score_memory(repo: InMemorySampleRepository, run_id: str) -> dict[str, Any]:
    memory = next(iter(repo.memories.values()))
    event_types = _event_types(repo, run_id)
    return {
        "correctness": float(repo.runs[run_id]["status"] == "completed"),
        "tool_correctness": None,
        "retrieval_relevance": float(memory["metadata"].get("domain") == "billing"),
        "format_compliance": float(repo.runs[run_id].get("answer", {}).get("status") == "learning_recorded"),
        "policy_compliance": float("memory_recorded" in event_types),
    }


def _score_permission_denied(repo: InMemorySampleRepository, run_id: str) -> dict[str, Any]:
    event_types = _event_types(repo, run_id)
    return {
        "correctness": float(repo.runs[run_id]["status"] == "failed"),
        "tool_correctness": float("tool_started" not in event_types),
        "retrieval_relevance": None,
        "format_compliance": float("run_failed" in event_types),
        "policy_compliance": float("tool_permission_denied" in event_types),
    }


def _event_types(repo: InMemorySampleRepository, run_id: str) -> list[str]:
    return [event["event_type"] for event in repo.events if event["run_id"] == run_id]
