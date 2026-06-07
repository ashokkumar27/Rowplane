"""Real Postgres example: tenant_boundary_search_isolation."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from rowplane.db.repository import PostgresRepository
from examples.use_cases.shared import (
    _score_tenant_boundary,
    _record_eval,
    _scenario_result,
    TENANT_ID,
    PostgresScenarioResult,
)

def run_tenant_boundary_search_isolation(conn: Any, repo: PostgresRepository) -> PostgresScenarioResult:
    other_tenant_id = "00000000-0000-0000-0000-000000000999"
    run_id = str(uuid4())
    other_run_id = str(uuid4())
    repo.create_run(TENANT_ID, run_id, {"request": "verify tenant-scoped search isolation"})
    repo.append_event(TENANT_ID, run_id, "tenant_boundary_checked", {"marker": "primary tenant marker"})
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", [other_tenant_id])
        cur.execute(
            """
            INSERT INTO agent_runs (id, tenant_id, status, task, model)
            VALUES (%s, %s, 'completed', '{}'::jsonb, 'sample-scripted-model')
            """,
            [other_run_id, other_tenant_id],
        )
        cur.execute(
            """
            INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
            VALUES (%s, %s, 'restricted_other_tenant_event', %s::jsonb, 'sample')
            """,
            [other_tenant_id, other_run_id, '{"marker":"other tenant secret marker"}'],
        )
        cur.execute("SELECT set_config('app.tenant_id', %s, false)", [TENANT_ID])
    repo.update_run_status(run_id, "queued", "thinking")
    repo.update_run_status(run_id, "thinking", "completed", answer={"status": "tenant_isolated"})
    scores = _score_tenant_boundary(conn, repo, run_id, other_run_id)
    _record_eval(repo, "tenant_boundary_search_isolation", run_id, scores)
    conn.commit()
    return _scenario_result(
        repo,
        run_id,
        "tenant_boundary_search_isolation",
        scores,
        ["Harness search returns primary-tenant evidence and excludes another tenant's marker."],
    )
