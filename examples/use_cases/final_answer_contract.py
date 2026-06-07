"""Real Postgres example: final_answer_contract."""

from __future__ import annotations

from typing import Any

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _scenario_harness,
    _record_eval,
    _scenario_result,
    PostgresScenarioResult,
)


def run_final_answer_contract(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    harness = _scenario_harness(
        conn,
        [
            {"action": "final", "answer": {"decision": "approve"}},
            {
                "action": "final",
                "answer": {
                    "decision": "approve",
                    "confidence": 0.93,
                    "evidence": ["final_answer_rejected correction used the run contract"],
                },
            },
        ],
    )
    run = harness.create_run(
        {"request": "Return a governed decision with evidence."},
        answer_contract={
            "schema": {
                "type": "object",
                "required": ["decision", "confidence", "evidence"],
                "properties": {
                    "decision": {"type": "string", "enum": ["approve", "reject", "escalate"]},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            }
        },
        max_iterations=4,
    )
    harness.drain_run(run.run_id, max_steps=4)
    scores = _score_final_answer_contract(repo, run.run_id)
    _record_eval(repo, "final_answer_contract", run.run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run.run_id,
        "final_answer_contract",
        scores,
        [
            "The first final answer is rejected by a dynamic answer_contract.",
            "The corrected final answer completes without hard-coded orchestration logic.",
        ],
    )


def _score_final_answer_contract(repo: PostgresRepository, run_id: str) -> dict[str, Any]:
    run = repo.load_run(run_id) or {}
    answer = run.get("answer") or {}
    events = repo.load_events(run_id, limit=200)
    event_types = [str(event["event_type"]) for event in events]
    return {
        "correctness": float(run.get("status") == "completed" and answer.get("decision") == "approve"),
        "tool_correctness": None,
        "retrieval_relevance": None,
        "format_compliance": float(
            isinstance(answer.get("confidence"), int | float)
            and isinstance(answer.get("evidence"), list)
        ),
        "policy_compliance": float("final_answer_rejected" in event_types and event_types.count("run_completed") == 1),
        "model_accounting": float(
            "model_call_reserved" in event_types and "model_call_completed" in event_types
        ),
    }
