"""Real Postgres example: customer_support_resolution."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _record_eval,
    _scenario_harness,
    _scenario_result,
    _score_customer_support_resolution,
    PostgresScenarioResult,
)


def run_customer_support_resolution(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {
                "action": "tool",
                "tool_name": "lookup_customer_context",
                "arguments": {"customer_id": "cust_789", "include_recent_cases": True},
            },
            {
                "action": "tool",
                "tool_name": "search_policy_documents",
                "arguments": {
                    "query": "Duplicate billing refund policy and retention-risk support handling.",
                    "top_k": 2,
                },
            },
            {
                "action": "tool",
                "tool_name": "issue_refund",
                "arguments": {
                    "customer_id": "cust_789",
                    "amount_cents": 4500,
                    "reason": "duplicate billing charge verified by support policy",
                },
            },
            {
                "action": "tool",
                "tool_name": "issue_refund",
                "arguments": {
                    "customer_id": "cust_789",
                    "amount_cents": 4500,
                    "reason": "duplicate billing charge verified by support policy",
                },
            },
            {
                "action": "tool",
                "tool_name": "create_support_ticket",
                "arguments": {
                    "customer_id": "cust_789",
                    "title": "Duplicate billing refund and retention follow-up",
                    "severity": "medium",
                },
            },
            {
                "action": "tool",
                "tool_name": "update_customer_case",
                "arguments": {
                    "case_id": "case_9001",
                    "customer_id": "cust_789",
                    "status": "resolved",
                    "resolution_summary": "Refund approved, ticket created, retention follow-up scheduled.",
                    "tags": ["billing", "refund", "retention-risk"],
                },
            },
            {
                "action": "remember",
                "memory_type": "case_learning",
                "content": "For enterprise duplicate billing cases, verify policy, approval-gate refunds, create a retention follow-up ticket, and record the resolution.",
                "metadata": {
                    "domain": "customer_support",
                    "case_id": "case_9001",
                    "customer_tier": "enterprise",
                },
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
                },
            },
        ],
    )
    run = harness.create_run(
        {
            "case_id": "case_9001",
            "customer_id": "cust_789",
            "message": "I was charged twice and may cancel if this is not fixed today.",
            "channel": "email",
        },
        max_iterations=12,
        required_capabilities=["support:tier1"],
        priority=50,
    )

    harness.drain_leased_work(
        worker_id="support-worker-1",
        kinds=["run"],
        capabilities=["support:tier1", "billing", "refunds"],
        max_steps=10,
    )
    approvals = run.approvals()
    if not approvals:
        raise RuntimeError("expected refund approval in customer support scenario")
    harness.approve(str(approvals[0]["id"]), resolved_by="support-lead")
    harness.drain_leased_work(
        worker_id="support-worker-2",
        kinds=["run"],
        capabilities=["support:tier1", "billing", "refunds"],
        max_steps=20,
    )

    scores = _score_customer_support_resolution(repo, run.run_id)
    _record_eval(repo, "customer_support_resolution", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "customer_support_resolution",
        scores,
        [
            "Developer path: one AgentHarness, normal @tool functions, deterministic ScriptedModel for repeatable tests.",
            "Scalable path: create_run plus drain_leased_work uses SQL leases and worker IDs instead of in-memory orchestration.",
            "Governance path: refund pauses for approval before the side-effecting handler runs.",
            "Adoption path: the same run can be inspected through run.explain(), events, tool_executions, approvals, memory, and eval_results.",
        ],
    )
