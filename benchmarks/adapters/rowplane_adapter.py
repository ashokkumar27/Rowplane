"""Rowplane benchmark adapter backed by real Postgres rows."""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import uuid4

from benchmarks.openai_client import OpenAIJsonClient
from benchmarks.scoring import score_run
from benchmarks.types import BenchmarkRunRecord, BenchmarkScenario, ToolCallRecord

from rowplane import AgentHarness, tool


@tool(
    name="search_policy_documents",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "additionalProperties": False,
    },
    description="Search enterprise policy documents.",
)
def bench_search_policy_documents(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "documents": [
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
        ][: int(arguments.get("top_k", 2))]
    }


@tool(
    name="issue_refund",
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
    description="Issue a benchmark refund after database approval.",
)
def bench_issue_refund(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "refund_issued",
        "refund_id": f"refund_{context.idempotency_key[:10]}",
        "customer_id": arguments["customer_id"],
        "amount_cents": arguments["amount_cents"],
    }


@tool(
    name="export_customer_data",
    input_schema={
        "type": "object",
        "required": ["scope"],
        "properties": {"scope": {"type": "string"}},
        "additionalProperties": False,
    },
    is_side_effecting=True,
    requires_approval=True,
    description="Export customer data. This benchmark intentionally withholds permission.",
)
def bench_export_customer_data(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"status": "should_not_execute", "scope": arguments["scope"]}


@tool(
    name="search_tenant_memory",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {"query": {"type": "string"}},
        "additionalProperties": False,
    },
    description="Search tenant memory.",
)
def bench_search_tenant_memory(context: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "memories": [
            {
                "tenant_id": "tenant_primary",
                "content": "primary tenant marker: billing duplicate charge guidance",
            }
        ]
    }


class RowplaneOpenAIModelClient:
    def __init__(self, record: BenchmarkRunRecord, *, model: str) -> None:
        self.record = record
        self.client = OpenAIJsonClient(model=model)

    def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        system = "\n".join(message["content"] for message in messages if message["role"] == "system")
        user = "\n".join(message["content"] for message in messages if message["role"] == "user")
        response = self.client.complete_json(system, user)
        self.record.input_tokens = (self.record.input_tokens or 0) + (response.usage.input_tokens or 0)
        self.record.output_tokens = (self.record.output_tokens or 0) + (response.usage.output_tokens or 0)
        if response.usage.estimated_cost_usd is not None:
            self.record.estimated_cost_usd = (self.record.estimated_cost_usd or 0.0) + response.usage.estimated_cost_usd
        return response.value


class RowplaneAdapter:
    name = "rowplane"

    def __init__(self, *, database_url: str | None = None) -> None:
        self.database_url = database_url or os.environ.get("DATABASE_URL")

    def run(self, scenario: BenchmarkScenario, *, repeat: int, model: str) -> BenchmarkRunRecord:
        record = BenchmarkRunRecord(
            framework=self.name,
            scenario=scenario.name,
            repeat=repeat,
            model=model,
        )
        started = time.perf_counter()
        if not self.database_url:
            record.errors.append("DATABASE_URL is required for rowplane benchmark runs")
            record.latency_ms = _elapsed_ms(started)
            record.score = score_run(record, scenario)
            return record

        tenant_id = str(uuid4())
        try:
            with AgentHarness(
                self.database_url,
                tenant_id=tenant_id,
                model_client=RowplaneOpenAIModelClient(record, model=model),
            ) as harness:
                harness.migrate()
                _register_scenario_tools(harness, scenario.name)
                task = {
                    "benchmark_prompt": scenario.prompt,
                    "available_tools": [
                        {**tool_spec.__dict__}
                        for tool_spec in scenario.tools
                        if tool_spec.name != "request_approval"
                    ],
                    "instructions": (
                        "Use exactly one Rowplane command at a time. "
                        "For approval-gated tools, call the side-effect tool; "
                        "the database will create and enforce approval."
                    ),
                    "expected_final_answer": scenario.expected,
                }
                run = harness.create_run(task, model=model, max_iterations=scenario.max_turns)
                harness.run_until_terminal(
                    run.run_id,
                    max_steps=max(scenario.max_turns * 3, 12),
                    max_approval_cycles=4,
                    approval_handler=lambda approval: True,
                    resolved_by="benchmark_human",
                )
                _collect_rowplane_evidence(record, harness, run.run_id)
        except Exception as exc:
            record.errors.append(str(exc))
        finally:
            record.latency_ms = _elapsed_ms(started)
            record.score = score_run(record, scenario)
        return record


def _register_scenario_tools(harness: AgentHarness, scenario_name: str) -> None:
    if scenario_name in {"policy_retrieval_qa", "multi_agent_refund_review"}:
        harness.register_tool(bench_search_policy_documents)
    if scenario_name in {"refund_approval", "multi_agent_refund_review"}:
        harness.register_tool(bench_issue_refund)
    if scenario_name == "permission_denied_safety":
        harness.register_tool(bench_export_customer_data, grant_to_tenant=False)
    if scenario_name == "tenant_memory_search":
        harness.register_tool(bench_search_tenant_memory)


def _collect_rowplane_evidence(record: BenchmarkRunRecord, harness: AgentHarness, run_id: str) -> None:
    run = harness.load_run(run_id) or {}
    events = harness.events(run_id, limit=800)
    approvals = harness.approvals(run_id)
    executions = harness.tool_executions(run_id)
    record.answer = run.get("answer") if isinstance(run.get("answer"), dict) else None
    record.approvals = [dict(approval) for approval in approvals]
    record.sql_evidence = [
        {"source": "agent_runs", "status": run.get("status"), "run_id": run_id},
        {"source": "agent_events", "count": len(events)},
        {"source": "tool_executions", "count": len(executions)},
        {"source": "approval_requests", "count": len(approvals)},
    ]
    for event in events:
        payload = event.get("payload") or {}
        record.trace_events.append(
            {
                "type": str(event.get("event_type")),
                "tool_name": payload.get("tool_name"),
                "result": payload.get("result") or payload,
            }
        )
        if event.get("event_type") == "llm_command_received" and payload.get("action") == "tool":
            record.tool_calls.append(
                ToolCallRecord(
                    name=str(payload.get("tool_name")),
                    arguments=payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
                )
            )
    for approval in approvals:
        record.tool_calls.append(
            ToolCallRecord(
                name="request_approval",
                arguments=approval.get("payload") if isinstance(approval.get("payload"), dict) else {},
                result={"status": approval.get("status")},
                approved=approval.get("status") == "approved",
            )
        )
    for execution in executions:
        result = execution.get("result") or {}
        output = result.get("output") if isinstance(result, dict) else None
        if isinstance(output, dict) and output.get("status") in {"refund_issued", "committed"}:
            record.side_effects.append(output)
        record.tool_calls.append(
            ToolCallRecord(
                name=str(execution.get("tool_name", "unknown")),
                arguments=execution.get("arguments") if isinstance(execution.get("arguments"), dict) else {},
                result=output if isinstance(output, dict) else result,
                approved=None,
                side_effect_committed=bool(isinstance(output, dict) and output.get("status") == "refund_issued"),
            )
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
