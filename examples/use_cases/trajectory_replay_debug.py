"""Real Postgres example: trajectory_replay_debug."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _score_trajectory_debug,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_trajectory_replay_debug(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {
                "action": "tool",
                "tool_name": "issue_refund",
                "arguments": {
                    "customer_id": "cust_789",
                    "amount_cents": 9000,
                    "reason": "manual exception",
                },
            }
        ],
    )
    run = harness.create_run({"request": "debug rejected approval trajectory"})
    harness.drain_run(run.run_id)
    approvals = run.approvals()
    if not approvals:
        raise RuntimeError("expected pending approval for replay debug scenario")
    harness.reject(str(approvals[0]["id"]), resolved_by="risk_reviewer")
    scores = _score_trajectory_debug(conn, repo, run.run_id)
    _record_eval(repo, "trajectory_replay_debug", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "trajectory_replay_debug",
        scores,
        [
            "Developer-standard harness.reject() creates the blocked approval trajectory.",
            "Rejected approval creates a blocked run that can be replayed and searched from SQL.",
        ],
    )
