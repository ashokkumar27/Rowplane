#!/usr/bin/env python3
"""Customer support resolution starter using Rowplane leases and approvals."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rowplane import AgentHarness, tool
from rowplane.samples.use_cases import ScriptedModel

TENANT_ID = "00000000-0000-0000-0000-000000000888"


@tool(
    input_schema={
        "type": "object",
        "required": ["customer_id"],
        "properties": {
            "customer_id": {"type": "string"},
            "include_recent_cases": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
    description="Look up customer context for support triage.",
)
def lookup_customer_context(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "customer_id": args["customer_id"],
        "tier": "enterprise",
        "account_health": "at_risk",
        "eligible_refund_cents": 4500,
        "recent_cases": ["billing dispute", "invoice correction"] if args.get("include_recent_cases") else [],
    }


@tool(
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}},
        "additionalProperties": False,
    },
    description="Search support policy documents.",
)
def search_support_policy(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "documents": [
            {
                "id": "support:refunds",
                "text": "Duplicate billing charges are refundable after account verification and approval.",
            },
            {
                "id": "support:retention",
                "text": "At-risk enterprise accounts require a retention follow-up ticket.",
            },
        ]
    }


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
    description="Issue a support refund after approval.",
)
def issue_refund(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "refund_id": f"refund_{ctx.idempotency_key[:10]}",
        "customer_id": args["customer_id"],
        "amount_cents": args["amount_cents"],
        "status": "issued",
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
    description="Create a follow-up support ticket.",
)
def create_support_ticket(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticket_id": f"ticket_{ctx.idempotency_key[:10]}",
        "customer_id": args["customer_id"],
        "ticket_status": "open",
        "severity": args["severity"],
    }


@tool(
    input_schema={
        "type": "object",
        "required": ["case_id", "customer_id", "status", "resolution_summary"],
        "properties": {
            "case_id": {"type": "string"},
            "customer_id": {"type": "string"},
            "status": {"type": "string", "enum": ["open", "resolved", "escalated"]},
            "resolution_summary": {"type": "string"},
        },
        "additionalProperties": False,
    },
    is_side_effecting=True,
    description="Update the support case record.",
)
def update_support_case(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_update_id": f"case_update_{ctx.idempotency_key[:10]}",
        "case_id": args["case_id"],
        "status": args["status"],
    }


def scripted_support_model() -> ScriptedModel:
    return ScriptedModel([
        {
            "action": "tool",
            "tool_name": "lookup_customer_context",
            "arguments": {"customer_id": "cust_789", "include_recent_cases": True},
        },
        {
            "action": "tool",
            "tool_name": "search_support_policy",
            "arguments": {"query": "duplicate billing refund and retention policy", "top_k": 2},
        },
        {
            "action": "tool",
            "tool_name": "issue_refund",
            "arguments": {
                "customer_id": "cust_789",
                "amount_cents": 4500,
                "reason": "duplicate billing charge verified by policy",
            },
        },
        {
            "action": "tool",
            "tool_name": "issue_refund",
            "arguments": {
                "customer_id": "cust_789",
                "amount_cents": 4500,
                "reason": "duplicate billing charge verified by policy",
            },
        },
        {
            "action": "tool",
            "tool_name": "create_support_ticket",
            "arguments": {
                "customer_id": "cust_789",
                "title": "Retention follow-up after duplicate billing refund",
                "severity": "medium",
            },
        },
        {
            "action": "tool",
            "tool_name": "update_support_case",
            "arguments": {
                "case_id": "case_9001",
                "customer_id": "cust_789",
                "status": "resolved",
                "resolution_summary": "Refund issued and retention follow-up opened.",
            },
        },
        {
            "action": "remember",
            "memory_type": "case_learning",
            "content": "Enterprise duplicate billing cases should verify policy, approval-gate refunds, and create retention follow-up.",
            "metadata": {"domain": "customer_support", "case_id": "case_9001"},
        },
        {
            "action": "final",
            "answer": {
                "status": "resolved_with_refund",
                "customer_id": "cust_789",
                "case_id": "case_9001",
                "refund_status": "issued",
                "ticket_status": "open",
                "next_step": "retention_follow_up",
                "evidence_tools": [
                    "lookup_customer_context",
                    "search_support_policy",
                    "issue_refund",
                    "create_support_ticket",
                    "update_support_case",
                ],
            },
        },
    ])


def live_support_model(model: str, max_output_tokens: int) -> Any:
    from rowplane.adapters import OpenAIModelClient

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required for --live")
    return OpenAIModelClient(
        model=model,
        api_key=api_key,
        max_output_tokens=max_output_tokens,
        estimated_call_cost_usd=0.02,
        input_cost_per_million=2.0,
        output_cost_per_million=8.0,
        instructions=(
            "Return exactly one JSON command and no prose. Use registered tool names exactly. "
            "Every tool.arguments object must exactly match its registered input_schema; never add extra keys. "
            "For issue_refund, use exactly customer_id, amount_cents, and reason. Do not include case_id. "
            "Do not use ask_human for tool approvals; Rowplane creates approval requests automatically. "
            "After approval is resolved, repeat the same issue_refund command so Rowplane can execute the approved side effect idempotently. "
            "If memory_recorded evidence is missing after tools complete, return a remember command before final. "
            "Do not invent refund_id or ticket_id values; report stable business status in final.answer."
        ),
        request_options={"metadata": {"app": "rowplane", "scenario": "customer_support_starter"}},
    )


def support_task(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "customer_id": "cust_789",
        "message": "I was charged twice and may cancel if this is not fixed today.",
        "goal": "Resolve the customer support case with governed evidence.",
        "workflow": [
            "Call lookup_customer_context for cust_789 with include_recent_cases=true.",
            "Call search_support_policy for duplicate billing refund and retention policy.",
            "If eligible, call issue_refund for 4500 cents. This will pause for approval.",
            "After approval is resolved, repeat the same issue_refund command so Rowplane can execute it idempotently.",
            "Call create_support_ticket for a retention follow-up.",
            "Call update_support_case with status resolved.",
            "Remember case_learning with metadata domain=customer_support and the case_id.",
            "Return final only after required tool, approval, and memory evidence exists.",
        ],
        "answer_contract": {
            "schema": {
                "type": "object",
                "required": [
                    "status",
                    "customer_id",
                    "case_id",
                    "refund_status",
                    "ticket_status",
                    "next_step",
                    "evidence_tools",
                ],
                "properties": {
                    "status": {"type": "string", "const": "resolved_with_refund"},
                    "customer_id": {"type": "string"},
                    "case_id": {"type": "string"},
                    "refund_status": {"type": "string", "const": "issued"},
                    "ticket_status": {"type": "string", "const": "open"},
                    "next_step": {"type": "string"},
                    "evidence_tools": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
            "required_tools": [
                "lookup_customer_context",
                "search_support_policy",
                "issue_refund",
                "create_support_ticket",
                "update_support_case",
            ],
            "required_event_types": ["memory_recorded"],
            "required_approval_status": "approved",
            "must_reference_tools": True,
            "min_tool_successes": 5,
        },
    }


def load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use OpenAIModelClient instead of ScriptedModel.")
    parser.add_argument("--model", default="gpt-5", help="OpenAI model for --live mode.")
    parser.add_argument("--max-output-tokens", type=int, default=2400, help="OpenAI max output tokens for --live mode.")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL/ROWPLANE_DATABASE_URL.")
    return parser


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or os.environ.get("ROWPLANE_DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or ROWPLANE_DATABASE_URL is required")

    model_client = live_support_model(args.model, args.max_output_tokens) if args.live else scripted_support_model()
    case_id = "case_live_9001" if args.live else "case_9001"

    with AgentHarness(database_url, tenant_id=TENANT_ID, model_client=model_client) as harness:
        harness.migrate()
        harness.set_budget(max_model_calls=50, max_tool_executions=25, max_estimated_cost_usd=5, max_active_work=4)
        for handler in (
            lookup_customer_context,
            search_support_policy,
            issue_refund,
            create_support_ticket,
            update_support_case,
        ):
            harness.register_tool(handler)

        run = harness.create_run(
            support_task(case_id),
            model=args.model if args.live else "sample-scripted-model",
            max_iterations=16,
            required_capabilities=["support:tier1"],
            priority=50,
        )

        harness.drain_leased_work(
            worker_id="support-worker-1",
            kinds=["run"],
            capabilities=["support:tier1", "billing", "refunds"],
            max_steps=12,
        )
        print("after first worker", run.explain())

        approvals = run.approvals()
        if approvals:
            harness.approve(str(approvals[0]["id"]), resolved_by="support-lead")

        harness.drain_leased_work(
            worker_id="support-worker-2",
            kinds=["run"],
            capabilities=["support:tier1", "billing", "refunds"],
            max_steps=24,
        )
        print("final", run.explain())
        print("tool executions", [item["tool_name"] for item in run.tool_executions()])
        print(
            "memory",
            harness.search_memory(
                memory_type="case_learning",
                metadata_contains={"domain": "customer_support"},
                source_run_id=run.run_id,
            ),
        )


if __name__ == "__main__":
    main()
