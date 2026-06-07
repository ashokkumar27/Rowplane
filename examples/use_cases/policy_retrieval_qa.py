"""Real Postgres example: policy_retrieval_qa."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _score_policy_qa,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_policy_retrieval_qa(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {
                "action": "tool",
                "tool_name": "search_policy_documents",
                "arguments": {"query": "enterprise data processing", "top_k": 2},
            },
            {
                "action": "final",
                "answer": {
                    "answer": "Use the DPA for data processing and SOC 2 for controls.",
                    "citations": ["policy:dpa", "policy:soc2"],
                },
            },
        ],
    )
    run = harness.create_run({"question": "What documents govern enterprise data processing?"})
    harness.drain_run(run.run_id)
    scores = _score_policy_qa(repo, run.run_id)
    _record_eval(repo, "policy_retrieval_qa", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "policy_retrieval_qa",
        scores,
        [
            "Developer-standard AgentHarness creates and queues the run.",
            "Real PGMQ + tool execution + final answer with citations.",
        ],
    )
