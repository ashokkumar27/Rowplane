"""Shared helpers for real Postgres example use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rowplane.approvals.service import ApprovalService
from rowplane.client import AgentHarness, as_tool_definition, tool
from rowplane.db.repository import PostgresRepository
from rowplane.evals.recorder import EvalRecorder, EvalScores
from rowplane.samples.use_cases import ScriptedModel
from rowplane.tools.executor import ToolExecutor
from rowplane.tools.task_executor import TaskToolExecutor
from rowplane.tools.registry import ToolRegistry
from rowplane.workers.task_worker import AgentTaskWorker
from rowplane.workers.worker import AgentWorker

ROOT = Path(__file__).resolve().parents[2]
TENANT_ID = "00000000-0000-0000-0000-000000000123"
SEED_FILE = ROOT / "db" / "seeds" / "sample_use_cases.sql"

@dataclass(frozen=True)
class PostgresScenarioResult:
    name: str
    run_id: str
    final_status: str
    scores: dict[str, Any]
    event_types: list[str]
    answer: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PostgresSuiteResult:
    scenarios: list[PostgresScenarioResult]
    harness_assessment: dict[str, Any]
    capability_matrix: dict[str, list[str]]


_REFUND_CALLS_BY_KEY: dict[str, int] = {}


def reset_sample_side_effect_state() -> None:
    _REFUND_CALLS_BY_KEY.clear()


@tool(
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer"},
        },
        "additionalProperties": False,
    },
    description="Search enterprise policy documents.",
)
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


@tool(
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
    description="Issue a customer refund after human approval.",
)
def issue_refund(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    _REFUND_CALLS_BY_KEY[context.idempotency_key] = _REFUND_CALLS_BY_KEY.get(context.idempotency_key, 0) + 1
    return {
        "refund_id": f"refund_{context.idempotency_key[:10]}",
        "customer_id": arguments["customer_id"],
        "amount_cents": arguments["amount_cents"],
        "handler_call_count": _REFUND_CALLS_BY_KEY[context.idempotency_key],
    }


@tool(
    input_schema={
        "type": "object",
        "required": ["scope"],
        "properties": {"scope": {"type": "string"}},
        "additionalProperties": False,
    },
    is_side_effecting=True,
    requires_approval=True,
    description="Export customer data. This sample intentionally has no tenant grant.",
)
def export_customer_data(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return {"export_id": "should_not_exist"}


@tool(
    input_schema={
        "type": "object",
        "required": ["service", "release", "incident_id"],
        "properties": {
            "service": {"type": "string"},
            "release": {"type": "string"},
            "incident_id": {"type": "string"},
        },
        "additionalProperties": False,
    },
    is_side_effecting=True,
    requires_approval=True,
    description="Rollback a production deployment after approval.",
)
def rollback_deployment(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "rollback_id": f"rollback_{context.idempotency_key[:10]}",
        "service": arguments["service"],
        "release": arguments["release"],
        "incident_id": arguments["incident_id"],
        "status": "rolled_back",
    }


@tool(
    input_schema={
        "type": "object",
        "required": ["customer_id", "title", "severity"],
        "properties": {
            "customer_id": {"type": "string"},
            "title": {"type": "string"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        },
        "additionalProperties": False,
    },
    is_side_effecting=True,
    description="Create a support ticket in an enterprise system.",
)
def create_support_ticket(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "ticket_id": f"ticket_{context.idempotency_key[:10]}",
        "customer_id": arguments["customer_id"],
        "ticket_status": "open",
        "severity": arguments["severity"],
        "title": arguments["title"],
    }


SAMPLE_TOOL_HANDLERS = (
    search_policy_documents,
    issue_refund,
    export_customer_data,
    rollback_deployment,
    create_support_ticket,
)


def _sample_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for handler in SAMPLE_TOOL_HANDLERS:
        registry.register(as_tool_definition(handler))
    return registry


def register_sample_tool_contracts(conn: Any) -> None:
    harness = AgentHarness.from_connection(conn, tenant_id=TENANT_ID, registry=_sample_registry())
    for handler in SAMPLE_TOOL_HANDLERS:
        definition = as_tool_definition(handler)
        harness.register_tool(
            handler,
            grant_to_tenant=definition.name != "export_customer_data",
        )


def _scenario_harness(conn: Any, responses: Sequence[Mapping[str, Any]]) -> AgentHarness:
    return AgentHarness.from_connection(
        conn,
        tenant_id=TENANT_ID,
        model_client=ScriptedModel(responses),
        registry=_sample_registry(),
    )


def _worker(repo: PostgresRepository, responses: Sequence[Mapping[str, Any]]) -> AgentWorker:
    return AgentWorker(
        repo,
        ScriptedModel(responses),
        ToolExecutor(repo, _sample_registry()),
    )


def _task_worker(repo: PostgresRepository, responses: Sequence[Mapping[str, Any]]) -> AgentTaskWorker:
    return AgentTaskWorker(
        repo,
        ScriptedModel(responses),
        TaskToolExecutor(repo, _sample_registry()),
    )

def _drain_worker(worker: AgentWorker, *, max_steps: int = 12) -> list[str]:
    outcomes: list[str] = []
    for _ in range(max_steps):
        outcome = worker.run_once()
        outcomes.append(outcome)
        if outcome == "empty":
            break
    return outcomes


def _drain_task_worker(worker: AgentTaskWorker, *, max_steps: int = 30) -> list[str]:
    outcomes: list[str] = []
    for _ in range(max_steps):
        outcome = worker.run_once()
        outcomes.append(outcome)
        if outcome == "empty":
            break
    return outcomes


def _record_eval(
    repo: PostgresRepository,
    eval_case_name: str,
    run_id: str,
    scores: Mapping[str, Any],
) -> None:
    EvalRecorder(repo).record(
        TENANT_ID,
        repo.get_eval_case_id(TENANT_ID, eval_case_name),
        run_id,
        EvalScores(
            correctness=scores.get("correctness"),
            tool_correctness=scores.get("tool_correctness"),
            retrieval_relevance=scores.get("retrieval_relevance"),
            format_compliance=scores.get("format_compliance"),
            policy_compliance=scores.get("policy_compliance"),
            extra={
                "runner": "postgres",
                "notes": scores.get("notes", []),
                "model_accounting": scores.get("model_accounting"),
            },
        ),
    )


def _scenario_result(
    repo: PostgresRepository,
    run_id: str,
    name: str,
    scores: dict[str, Any],
    notes: list[str],
) -> PostgresScenarioResult:
    run = repo.load_run(run_id)
    if run is None:
        raise RuntimeError(f"run disappeared: {run_id}")
    events = repo.load_events(run_id, limit=500)
    return PostgresScenarioResult(
        name=name,
        run_id=run_id,
        final_status=str(run["status"]),
        answer=run.get("answer"),
        event_types=[str(event["event_type"]) for event in events],
        scores=scores,
        notes=notes,
    )


def _score_policy_qa(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id)
    answer = (run or {}).get("answer") or {}
    citations = answer.get("citations", [])
    event_types = _event_types(repo, run_id)
    return {
        "correctness": float(
            (run or {}).get("status") == "completed"
            and "DPA" in answer.get("answer", "")
            and "SOC 2" in answer.get("answer", "")
        ),
        "tool_correctness": float("tool_completed" in event_types),
        "retrieval_relevance": float({"policy:dpa", "policy:soc2"}.issubset(set(citations))),
        "format_compliance": float(isinstance(citations, list) and len(citations) == 2),
        "policy_compliance": 1.0,
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _score_refund(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    event_types = _event_types(repo, run_id)
    approval_index = event_types.index("approval_requested")
    tool_started_index = event_types.index("tool_started")
    executions = repo.list_tool_executions(run_id)
    completed = [item for item in executions if item["status"] == "completed"]
    result = completed[0]["result"]["output"] if completed else {}
    return {
        "correctness": float(run.get("status") == "completed"),
        "tool_correctness": float(result.get("handler_call_count") == 1),
        "retrieval_relevance": None,
        "format_compliance": float((run.get("answer") or {}).get("status") == "refund_issued"),
        "policy_compliance": float(approval_index < tool_started_index),
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _score_memory(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    event_types = _event_types(repo, run_id)
    with repo.conn.cursor() as cur:
        cur.execute(
            """
            SELECT metadata FROM agent_memory
            WHERE tenant_id = %s AND source_run_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [TENANT_ID, run_id],
        )
        memory = cur.fetchone()
    metadata = memory["metadata"] if memory else {}
    return {
        "correctness": float(run.get("status") == "completed"),
        "tool_correctness": None,
        "retrieval_relevance": float(metadata.get("domain") == "billing"),
        "format_compliance": float((run.get("answer") or {}).get("status") == "learning_recorded"),
        "policy_compliance": float("memory_recorded" in event_types),
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _score_permission_denied(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    event_types = _event_types(repo, run_id)
    return {
        "correctness": float(run.get("status") == "failed"),
        "tool_correctness": float("tool_started" not in event_types),
        "retrieval_relevance": None,
        "format_compliance": float("run_failed" in event_types),
        "policy_compliance": float("tool_permission_denied" in event_types),
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _score_multi_agent_refund_review(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    answer = run.get("answer") or {}
    events = repo.load_events(run_id, limit=800)
    event_types = [str(event["event_type"]) for event in events]
    executions = repo.list_tool_executions(run_id)
    completed = [item for item in executions if item["status"] == "completed"]
    refund_results = [
        item["result"]["output"]
        for item in completed
        if (item.get("result") or {}).get("output", {}).get("customer_id") == "cust_123"
    ]
    citations = answer.get("citations", [])
    delegation_count = event_types.count("delegation_created")
    approval_index = _first_event_index(events, "approval_requested", tool_name="issue_refund")
    refund_started_index = _first_event_index(events, "tool_started", tool_name="issue_refund")
    with repo.conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS task_count
            FROM agent_tasks
            WHERE tenant_id = %s AND run_id = %s
            """,
            [TENANT_ID, run_id],
        )
        task_count = int(cur.fetchone()["task_count"])
    return {
        "correctness": float(run.get("status") == "completed" and answer.get("status") == "refund_issued"),
        "tool_correctness": float(bool(refund_results) and refund_results[0].get("handler_call_count") == 1),
        "retrieval_relevance": float({"policy:dpa", "policy:soc2"}.issubset(set(citations))),
        "format_compliance": float(
            answer.get("review") == "approved"
            and isinstance(answer.get("agents"), list)
            and delegation_count >= 3
            and task_count >= 4
        ),
        "policy_compliance": float(
            approval_index is not None
            and refund_started_index is not None
            and approval_index < refund_started_index
            and "tool_permission_denied" not in event_types
        ),
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _score_sql_schema_guardrail(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    event_types = _event_types(repo, run_id)
    return {
        "correctness": float(run.get("status") == "failed"),
        "tool_correctness": float("tool_validation_failed" in event_types and "tool_started" not in event_types),
        "retrieval_relevance": None,
        "format_compliance": float("run_failed" in event_types),
        "policy_compliance": 1.0,
        "model_accounting": 0.0,
    }


def _score_sre_rollback(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    event_types = _event_types(repo, run_id)
    executions = repo.list_tool_executions(run_id)
    completed = [item for item in executions if item["status"] == "completed"]
    result = completed[0]["result"]["output"] if completed else {}
    approval_index = event_types.index("approval_requested")
    tool_started_index = event_types.index("tool_started")
    return {
        "correctness": float(run.get("status") == "completed" and (run.get("answer") or {}).get("status") == "rollback_completed"),
        "tool_correctness": float(result.get("status") == "rolled_back" and len(completed) == 1),
        "retrieval_relevance": None,
        "format_compliance": float((run.get("answer") or {}).get("incident_id") == "inc_500"),
        "policy_compliance": float(approval_index < tool_started_index),
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _score_state_diff_ticket(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    event_types = _event_types(repo, run_id)
    executions = repo.list_tool_executions(run_id)
    completed = [item for item in executions if item["status"] == "completed"]
    result = completed[0]["result"]["output"] if completed else {}
    expected = (run.get("answer") or {}).get("expected_state") or {}
    state_matches = result.get("ticket_status") == expected.get("ticket_status") and result.get("severity") == expected.get("severity")
    return {
        "correctness": float(run.get("status") == "completed" and state_matches),
        "tool_correctness": float(result.get("ticket_id", "").startswith("ticket_")),
        "retrieval_relevance": None,
        "format_compliance": float((run.get("answer") or {}).get("status") == "ticket_created"),
        "policy_compliance": 1.0,
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _score_tenant_boundary(conn: Any, repo: PostgresRepository, run_id: str, other_run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    primary = _search_harness(conn, TENANT_ID, "primary tenant marker")
    leaked = _search_harness(conn, TENANT_ID, "other tenant secret marker")
    other = _search_harness(conn, "00000000-0000-0000-0000-000000000999", "other tenant secret marker")
    return {
        "correctness": float(run.get("status") == "completed"),
        "tool_correctness": None,
        "retrieval_relevance": float(bool(primary) and bool(other)),
        "format_compliance": float((run.get("answer") or {}).get("status") == "tenant_isolated"),
        "policy_compliance": float(not leaked and bool(other) and str(other[0].get("run_id")) == other_run_id),
        "model_accounting": 0.0,
    }


def _score_trajectory_debug(conn: Any, repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    trajectory = _run_trajectory(conn, TENANT_ID, run_id)
    search = _search_harness(conn, TENANT_ID, "approval rejected")
    event_types = [row["step_type"] for row in trajectory if row["source"] == "event"]
    return {
        "correctness": float(run.get("status") == "blocked"),
        "tool_correctness": float("tool_started" not in event_types),
        "retrieval_relevance": float(bool(search)),
        "format_compliance": float("approval_resolved" in event_types and "run_status_changed" in event_types),
        "policy_compliance": float("approval_requested" in event_types and "tool_completed" not in event_types),
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }


def _sql_decision(conn: Any, sql: str, params: Sequence[Any]) -> Mapping[str, Any]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("SQL runtime function returned no row")
    return next(iter(row.values()))


def _run_trajectory(conn: Any, tenant_id: str, run_id: str) -> list[Mapping[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, step_type, payload
            FROM app.run_trajectory_v
            WHERE tenant_id = %s AND run_id = %s
            ORDER BY created_at, sequence_id
            """,
            [tenant_id, run_id],
        )
        return cur.fetchall()


def _search_harness(conn: Any, tenant_id: str, query: str) -> list[Mapping[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, id, run_id, snippet, payload FROM app.search_harness(%s::uuid, %s, 20)",
            [tenant_id, query],
        )
        return cur.fetchall()


def _first_event_index(
    events: Sequence[Mapping[str, Any]],
    event_type: str,
    *,
    tool_name: str | None = None,
) -> int | None:
    for index, event in enumerate(events):
        if event["event_type"] != event_type:
            continue
        payload = event.get("payload") or {}
        if tool_name is not None and payload.get("tool_name") != tool_name:
            continue
        return index
    return None


def _event_types(repo: PostgresRepository, run_id: str) -> list[str]:
    return [str(event["event_type"]) for event in repo.load_events(run_id, limit=500)]
