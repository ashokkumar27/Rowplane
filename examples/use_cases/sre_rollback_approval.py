"""Real Postgres example: sre_rollback_approval."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _score_sre_rollback,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_sre_rollback_approval(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {
                "action": "tool",
                "tool_name": "rollback_deployment",
                "arguments": {
                    "service": "checkout",
                    "release": "2026.06.02.1",
                    "incident_id": "inc_500",
                },
            },
            {
                "action": "tool",
                "tool_name": "rollback_deployment",
                "arguments": {
                    "service": "checkout",
                    "release": "2026.06.02.1",
                    "incident_id": "inc_500",
                },
            },
            {
                "action": "final",
                "answer": {
                    "status": "rollback_completed",
                    "incident_id": "inc_500",
                    "service": "checkout",
                },
            },
        ],
    )
    run = harness.create_run(
        {"incident_id": "inc_500", "service": "checkout", "release": "2026.06.02.1"}
    )
    harness.drain_run(run.run_id)
    approvals = run.approvals()
    if not approvals:
        raise RuntimeError("expected pending approval for SRE rollback scenario")
    harness.approve(str(approvals[0]["id"]), resolved_by="sre_manager")
    harness.drain_run(run.run_id)
    scores = _score_sre_rollback(repo, run.run_id)
    _record_eval(repo, "sre_rollback_approval", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "sre_rollback_approval",
        scores,
        [
            "Developer-standard approval API is used for a production rollback flow.",
            "SRE rollback is approval-gated and idempotent before a production side effect runs.",
        ],
    )
