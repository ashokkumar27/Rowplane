"""Real Postgres example: case_learning_memory."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _score_memory,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_case_learning_memory(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {
                "action": "remember",
                "memory_type": "case_learning",
                "content": "Escalate billing disputes above $100 to specialist review.",
                "metadata": {"case_id": "case_42", "domain": "billing"},
            },
            {
                "action": "final",
                "answer": {"status": "learning_recorded", "case_id": "case_42"},
            },
        ],
    )
    run = harness.create_run({"case_id": "case_42", "outcome": "escalate"})
    harness.drain_run(run.run_id)
    scores = _score_memory(repo, run.run_id)
    _record_eval(repo, "case_learning_memory", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "case_learning_memory",
        scores,
        [
            "Developer-standard AgentHarness drives the memory workflow.",
            "Real agent_memory write is linked to the run and audited.",
        ],
    )
