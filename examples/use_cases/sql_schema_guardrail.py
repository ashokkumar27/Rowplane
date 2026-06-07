"""Real Postgres example: sql_schema_guardrail."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _sql_decision,
    _score_sql_schema_guardrail,
    _record_eval,
    _scenario_result,
    TENANT_ID,
    PostgresScenarioResult,
)

def run_sql_schema_guardrail(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    run_id = str(uuid4())
    repo.create_run(TENANT_ID, run_id, {"request": "search policies with malformed arguments"})
    repo.update_run_status(run_id, "queued", "thinking")
    decision = _sql_decision(
        conn,
        """
        SELECT app.reserve_tool_execution(
          %s::uuid, %s::uuid, NULL::uuid, NULL::uuid,
          'search_policy_documents', %s::jsonb, false, 'sample_sql_runtime'
        )
        """,
        [TENANT_ID, run_id, '{"query":"refund","unexpected":true}'],
    )
    if decision.get("decision") != "validation_failed":
        raise RuntimeError(f"expected validation_failed decision, got {decision}")
    repo.append_event(
        TENANT_ID,
        run_id,
        "run_failed",
        {"reason": "database rejected malformed tool arguments", "decision": decision},
    )
    repo.update_run_status(run_id, "thinking", "failed", error="database rejected malformed tool arguments")
    scores = _score_sql_schema_guardrail(repo, run_id)
    _record_eval(repo, "sql_schema_guardrail", run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run_id,
        "sql_schema_guardrail",
        scores,
        ["Direct SQL runtime call proves database-enforced tool schema rejection."],
    )
