"""Real Postgres example: refund_approval."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _score_refund,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_refund_approval(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
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
        ],
    )
    run = harness.create_run({"customer_id": "cust_123", "amount_cents": 2500})
    harness.drain_run(run.run_id)
    approvals = run.approvals()
    if not approvals:
        raise RuntimeError("expected pending approval for refund scenario")
    harness.approve(str(approvals[0]["id"]), resolved_by="sample_human_approver")
    harness.drain_run(run.run_id)
    scores = _score_refund(repo, run.run_id)
    _record_eval(repo, "refund_approval", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "refund_approval",
        scores,
        [
            "Developer-standard run.approvals() and harness.approve() resolve the gate.",
            "Real approval row gates side-effecting tool execution.",
        ],
    )
