"""Real Postgres example: enterprise_state_diff_ticket."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _score_state_diff_ticket,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_enterprise_state_diff_ticket(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {
                "action": "tool",
                "tool_name": "create_support_ticket",
                "arguments": {
                    "customer_id": "cust_456",
                    "title": "SLA breach follow-up",
                    "severity": "high",
                },
            },
            {
                "action": "final",
                "answer": {
                    "status": "ticket_created",
                    "customer_id": "cust_456",
                    "expected_state": {"ticket_status": "open", "severity": "high"},
                },
            },
        ],
    )
    run = harness.create_run({"customer_id": "cust_456", "issue": "SLA breach", "severity": "high"})
    harness.drain_run(run.run_id)
    scores = _score_state_diff_ticket(repo, run.run_id)
    _record_eval(repo, "enterprise_state_diff_ticket", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "enterprise_state_diff_ticket",
        scores,
        [
            "Developer-standard @tool handler models an enterprise API side effect.",
            "Eval checks expected Postgres tool-execution state, not only final text.",
        ],
    )
