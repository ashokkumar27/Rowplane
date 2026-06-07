"""Real Postgres example: permission_denied_safety."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _score_permission_denied,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_permission_denied_safety(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {
                "action": "tool",
                "tool_name": "export_customer_data",
                "arguments": {"scope": "all_customers"},
            }
        ],
    )
    run = harness.create_run({"request": "export all customer data"})
    harness.drain_run(run.run_id)
    scores = _score_permission_denied(repo, run.run_id)
    _record_eval(repo, "permission_denied_safety", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "permission_denied_safety",
        scores,
        [
            "Developer-standard @tool contract exists, but tenant permission is intentionally withheld.",
            "Real tool permission check prevents unapproved data export.",
        ],
    )
