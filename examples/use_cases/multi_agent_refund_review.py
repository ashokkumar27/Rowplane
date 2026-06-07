"""Real Postgres example: multi_agent_refund_review."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    ApprovalService,
    _task_worker,
    _drain_task_worker,
    _score_multi_agent_refund_review,
    _record_eval,
    _scenario_result,
    TENANT_ID,
    PostgresScenarioResult,
)

def run_multi_agent_refund_review(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    run_id = str(uuid4())
    run = repo.create_run(
        TENANT_ID,
        run_id,
        {
            "customer_id": "cust_123",
            "amount_cents": 2500,
            "reason": "duplicate charge",
            "question": "Can we refund this duplicate charge under policy?",
        },
        max_iterations=16,
    )
    planner = repo.get_agent_by_name(TENANT_ID, "planner")
    if planner is None:
        raise RuntimeError("planner agent seed was not created")
    root_task = repo.create_agent_task(
        TENANT_ID,
        run_id,
        str(planner["id"]),
        run["task"],
        max_iterations=10,
    )
    repo.append_event(
        TENANT_ID,
        run_id,
        "task_created",
        {"task_id": str(root_task["id"]), "agent_id": str(planner["id"]), "root": True},
    )
    repo.queue_task(TENANT_ID, run_id, str(root_task["id"]))

    worker = _task_worker(
        repo,
        [
            {
                "action": "delegate",
                "to_agent": "policy_researcher",
                "task": {
                    "question": "Find approved policy evidence for duplicate charge refunds and data processing controls.",
                    "required_citations": ["policy:dpa", "policy:soc2"],
                },
                "reason": "Need grounded policy evidence before issuing a refund.",
            },
            {
                "action": "tool",
                "tool_name": "search_policy_documents",
                "arguments": {"query": "enterprise duplicate charge refund data processing controls", "top_k": 2},
            },
            {
                "action": "final",
                "answer": {
                    "summary": "The DPA governs data processing and SOC 2 covers operating controls relevant to the refund workflow.",
                    "citations": ["policy:dpa", "policy:soc2"],
                },
            },
            {
                "action": "delegate",
                "to_agent": "refund_operator",
                "task": {
                    "customer_id": "cust_123",
                    "amount_cents": 2500,
                    "reason": "duplicate charge",
                },
                "reason": "Issue the refund only through the registered approval-gated tool.",
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
            {
                "action": "delegate",
                "to_agent": "critic",
                "task": {
                    "check": "Confirm policy citations, approval gating, idempotent refund execution, and final answer format.",
                },
                "reason": "Need an independent governance review before finalizing.",
            },
            {
                "action": "final",
                "answer": {
                    "review": "approved",
                    "findings": [
                        "policy evidence cited",
                        "refund tool was approval gated",
                        "refund execution was idempotent",
                    ],
                },
            },
            {
                "action": "final",
                "answer": {
                    "status": "refund_issued",
                    "customer_id": "cust_123",
                    "amount_cents": 2500,
                    "citations": ["policy:dpa", "policy:soc2"],
                    "review": "approved",
                    "agents": ["policy_researcher", "refund_operator", "critic"],
                },
            },
        ],
    )

    for _ in range(12):
        outcome = worker.run_once()
        if outcome == "waiting_approval":
            break
        if outcome == "empty":
            raise RuntimeError("multi-agent scenario ended before approval was requested")
    approval = repo.pending_approval_for_run(run_id)
    if approval is None:
        raise RuntimeError("expected pending approval for multi-agent refund scenario")
    ApprovalService(repo).resolve(
        str(approval["id"]),
        approved=True,
        resolved_by="sample_human_approver",
    )
    _drain_task_worker(worker)

    scores = _score_multi_agent_refund_review(repo, run_id)
    _record_eval(repo, "multi_agent_refund_review", run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run_id,
        "multi_agent_refund_review",
        scores,
        [
            "Root planner delegates to researcher, refund operator, and critic using real agent_tasks.",
            "The refund operator is gated by task-scoped approval and idempotent tool execution.",
        ],
    )
