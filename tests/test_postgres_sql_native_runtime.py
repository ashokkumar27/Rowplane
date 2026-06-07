from __future__ import annotations

import os
import unittest
from uuid import uuid4

from helpers import SRC  # noqa: F401

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/rowplane"
TENANT_ID = "00000000-0000-0000-0000-000000000321"


class PostgresSqlNativeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import psycopg
        from psycopg.rows import dict_row

        from examples.postgres_showcase import reset_sample_database
        from rowplane.db.migrations import apply_migrations

        cls.database_url = os.environ.get("ROWPLANE_DATABASE_URL") or os.environ.get("PG_AGENT_DATABASE_URL", DEFAULT_DATABASE_URL)
        with psycopg.connect(cls.database_url, row_factory=dict_row, autocommit=False) as conn:
            reset_sample_database(conn)
            conn.commit()
            apply_migrations(conn)
            conn.commit()

    def setUp(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.conn = psycopg.connect(self.database_url, row_factory=dict_row, autocommit=False)
        self.conn.execute("SELECT set_config('app.tenant_id', %s, false)", [TENANT_ID])
        self._clear_tenant()

    def tearDown(self) -> None:
        self.conn.rollback()
        self.conn.close()

    def test_submit_final_command_completes_run_and_writes_events(self) -> None:
        run_id = self._create_run(status="thinking")

        decision = self._scalar(
            "SELECT app.submit_agent_command(%s::uuid, %s::uuid, NULL::uuid, NULL::uuid, %s::jsonb, 'test')",
            [TENANT_ID, run_id, '{"action":"final","answer":{"ok":true}}'],
        )

        self.assertEqual(decision["decision"], "completed")
        run = self._one("SELECT status, answer FROM agent_runs WHERE id = %s", [run_id])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["answer"], {"ok": True})
        self.assertEqual(
            self._event_types(run_id),
            ["llm_command_received", "run_completed", "run_status_changed"],
        )

    def test_validate_agent_command_rejects_bad_shape_and_bad_delegate_target(self) -> None:
        with self.assertRaises(Exception):
            self._scalar("SELECT app.validate_agent_command(%s::jsonb, true)", ['{"action":"final","answer":{},"extra":1}'])
        with self.assertRaises(Exception):
            self._scalar("SELECT app.validate_agent_command(%s::jsonb, true)", ['{"action":"delegate","to_agent":"BadAgent","task":{},"reason":"x"}'])

    def test_tool_reservation_denies_missing_permission_and_records_event(self) -> None:
        run_id = self._create_run(status="thinking")
        self._create_tool("search_policy_documents")

        decision = self._reserve_tool(run_id, "search_policy_documents", {"query": "refund"})

        self.assertEqual(decision["decision"], "permission_denied")
        self.assertEqual(self._event_types(run_id), ["tool_permission_denied"])

    def test_tool_reservation_enforces_database_input_schema(self) -> None:
        run_id = self._create_run(status="thinking")
        tool_id = self._create_tool(
            "search_policy_documents",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
        )
        self._grant_tenant(tool_id)

        missing = self._reserve_tool(run_id, "search_policy_documents", {})
        extra = self._reserve_tool(run_id, "search_policy_documents", {"query": "refund", "x": 1})

        self.assertEqual(missing["decision"], "validation_failed")
        self.assertEqual(extra["decision"], "validation_failed")
        self.assertEqual(self._event_types(run_id), ["tool_validation_failed", "tool_validation_failed"])

    def test_approval_then_completion_requeues_run_and_is_idempotent(self) -> None:
        run_id = self._create_run(status="thinking")
        tool_id = self._create_tool("issue_refund", requires_approval=True)
        self._grant_tenant(tool_id)

        waiting = self._reserve_tool(run_id, "issue_refund", {"amount": 25})
        self.assertEqual(waiting["decision"], "waiting_approval")
        self.assertEqual(self._status("agent_runs", run_id), "waiting_approval")

        resolved = self._scalar(
            "SELECT app.resolve_approval_request(%s::uuid, true, 'human_1')",
            [waiting["approval_request_id"]],
        )
        self.assertEqual(resolved["decision"], "resolved")
        self.assertEqual(self._status("agent_runs", run_id), "queued")

        self.conn.execute("UPDATE agent_runs SET status = 'thinking' WHERE id = %s", [run_id])
        executable = self._reserve_tool(run_id, "issue_refund", {"amount": 25})
        self.assertEqual(executable["decision"], "execute_tool")
        self.assertEqual(self._status("agent_runs", run_id), "tool_running")

        completed = self._scalar(
            "SELECT app.complete_tool_execution(%s::uuid, true, %s::jsonb, NULL, 'worker')",
            [executable["tool_execution_id"], '{"output":{"refund_id":"r1"},"metadata":{}}'],
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self._status("agent_runs", run_id), "queued")

        self.conn.execute("UPDATE agent_runs SET status = 'thinking' WHERE id = %s", [run_id])
        replay = self._reserve_tool(run_id, "issue_refund", {"amount": 25})
        self.assertEqual(replay["decision"], "replayed")
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(self._status("agent_runs", run_id), "queued")
        self.assertEqual(self._count("tool_executions", run_id), 1)

    def test_task_scoped_tool_reservation_uses_agent_permission(self) -> None:
        run_id = self._create_run(status="thinking")
        agent_id = self._create_agent("refund_operator")
        task_id = self._create_task(run_id, agent_id, status="thinking")
        tool_id = self._create_tool("issue_refund")
        self._grant_agent(tool_id, agent_id)

        executable = self._reserve_tool(run_id, "issue_refund", {"amount": 10}, task_id=task_id, agent_id=agent_id)
        self.assertEqual(executable["decision"], "execute_tool")
        self.assertEqual(self._status("agent_tasks", task_id), "tool_running")

        self._scalar(
            "SELECT app.complete_tool_execution(%s::uuid, false, '{}'::jsonb, 'handler failed', 'worker')",
            [executable["tool_execution_id"]],
        )
        self.assertEqual(self._status("agent_tasks", task_id), "queued")
        self.assertIn("tool_failed", self._event_types(run_id))


    def test_database_schema_subset_enforces_contract_details(self) -> None:
        valid = self._scalar(
            "SELECT app.jsonb_matches_schema(%s::jsonb, %s::jsonb)",
            [
                '{"decision":"approve","confidence":0.8,"evidence":["policy:dpa"]}',
                '{"type":"object","required":["decision","confidence","evidence"],"properties":{"decision":{"const":"approve"},"confidence":{"type":"number","minimum":0,"maximum":1},"evidence":{"type":"array","items":{"type":"string"}}},"additionalProperties":false}',
            ],
        )
        too_high = self._scalar(
            "SELECT app.jsonb_matches_schema(%s::jsonb, %s::jsonb)",
            [
                '{"decision":"approve","confidence":1.5,"evidence":["policy:dpa"]}',
                '{"type":"object","required":["decision","confidence","evidence"],"properties":{"decision":{"const":"approve"},"confidence":{"type":"number","minimum":0,"maximum":1},"evidence":{"type":"array","items":{"type":"string"}}},"additionalProperties":false}',
            ],
        )
        bad_item = self._scalar(
            "SELECT app.jsonb_matches_schema(%s::jsonb, %s::jsonb)",
            [
                '{"decision":"approve","confidence":0.8,"evidence":[123]}',
                '{"type":"object","required":["decision","confidence","evidence"],"properties":{"decision":{"const":"approve"},"confidence":{"type":"number","minimum":0,"maximum":1},"evidence":{"type":"array","items":{"type":"string"}}},"additionalProperties":false}',
            ],
        )

        self.assertTrue(valid)
        self.assertFalse(too_high)
        self.assertFalse(bad_item)

    def test_claim_agent_work_respects_tenant_active_work_budget(self) -> None:
        self._create_run(status="queued")
        self._create_run(status="queued")
        self.conn.execute(
            "INSERT INTO agent_runtime_budgets (tenant_id, scope_type, scope_id, max_active_work) VALUES (%s, 'tenant', %s, 1)",
            [TENANT_ID, TENANT_ID],
        )

        first = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY[]::text[], 10, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )
        second = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_2', ARRAY[]::text[], 10, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_claim_agent_work_records_run_active_work_budget_denial(self) -> None:
        run_id = self._create_run(status="queued")
        self.conn.execute(
            "INSERT INTO agent_runtime_budgets (tenant_id, scope_type, scope_id, max_active_work) VALUES (%s, 'run', %s, 0)",
            [TENANT_ID, run_id],
        )

        claimed = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY[]::text[], 1, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(claimed, [])
        self.assertIn("runtime_budget_exceeded", self._event_types(run_id))

    def test_reserve_model_call_records_allowed_reservation(self) -> None:
        run_id = self._create_run(status="thinking")
        self.conn.execute(
            "INSERT INTO agent_runtime_budgets (tenant_id, scope_type, scope_id, max_model_calls) VALUES (%s, 'run', %s, 1)",
            [TENANT_ID, run_id],
        )

        decision = self._scalar(
            "SELECT app.reserve_model_call(%s::uuid, %s::uuid, NULL::uuid, NULL::uuid, 'test-model', 'test')",
            [TENANT_ID, run_id],
        )

        self.assertEqual(decision["decision"], "allowed")
        self.assertEqual(decision["status"], "reserved")
        self.assertEqual(self._event_types(run_id), ["runtime_budget_checked", "model_call_reserved"])
        usage = self._scalar(
            "SELECT app.runtime_budget_scope_usage(%s::uuid, 'run', %s, 'model_calls')",
            [TENANT_ID, run_id],
        )
        self.assertEqual(usage, 1)

    def test_reserve_model_call_denies_budget_excess_before_external_call(self) -> None:
        run_id = self._create_run(status="thinking")
        self.conn.execute(
            "INSERT INTO agent_runtime_budgets (tenant_id, scope_type, scope_id, max_model_calls) VALUES (%s, 'run', %s, 0)",
            [TENANT_ID, run_id],
        )

        decision = self._scalar(
            "SELECT app.reserve_model_call(%s::uuid, %s::uuid, NULL::uuid, NULL::uuid, 'test-model', 'test')",
            [TENANT_ID, run_id],
        )

        self.assertEqual(decision["decision"], "denied")
        self.assertEqual(decision["reason"], "model_call_budget_exceeded")
        self.assertEqual(self._event_types(run_id), ["runtime_budget_exceeded", "model_call_denied_by_budget"])

    def test_complete_model_call_records_usage_and_cost(self) -> None:
        run_id = self._create_run(status="thinking")

        decision = self._scalar(
            "SELECT app.complete_model_call(%s::uuid, %s::uuid, NULL::uuid, NULL::uuid, 'test-model', 'completed', 123, 10, 5, 15, 0.25, NULL, 'test')",
            [TENANT_ID, run_id],
        )

        self.assertEqual(decision["decision"], "recorded")
        self.assertEqual(decision["event_type"], "model_call_completed")
        self.assertEqual(self._event_types(run_id), ["model_call_completed"])
        usage = self._scalar(
            "SELECT app.runtime_cost_budget_scope_usage(%s::uuid, 'run', %s)",
            [TENANT_ID, run_id],
        )
        self.assertEqual(float(usage), 0.25)

    def test_reserve_model_call_denies_projected_cost_budget(self) -> None:
        run_id = self._create_run(status="thinking")
        self.conn.execute(
            "INSERT INTO agent_runtime_budgets (tenant_id, scope_type, scope_id, max_estimated_cost_usd) VALUES (%s, 'run', %s, 0.10)",
            [TENANT_ID, run_id],
        )

        decision = self._scalar(
            "SELECT app.reserve_model_call(%s::uuid, %s::uuid, NULL::uuid, NULL::uuid, 'test-model', 'test', 0.11)",
            [TENANT_ID, run_id],
        )

        self.assertEqual(decision["decision"], "denied")
        self.assertEqual(decision["reason"], "model_cost_budget_exceeded")
        self.assertEqual(self._event_types(run_id), ["runtime_budget_exceeded", "model_call_denied_by_budget"])

    def test_reserve_model_call_uses_completed_cost_before_next_call(self) -> None:
        first_run_id = self._create_run(status="thinking")
        second_run_id = self._create_run(status="thinking")
        self.conn.execute(
            "INSERT INTO agent_runtime_budgets (tenant_id, scope_type, scope_id, max_estimated_cost_usd) VALUES (%s, 'tenant', %s, 0.30)",
            [TENANT_ID, TENANT_ID],
        )
        self._scalar(
            "SELECT app.complete_model_call(%s::uuid, %s::uuid, NULL::uuid, NULL::uuid, 'test-model', 'completed', 100, 10, 10, 20, 0.25, NULL, 'test')",
            [TENANT_ID, first_run_id],
        )

        decision = self._scalar(
            "SELECT app.reserve_model_call(%s::uuid, %s::uuid, NULL::uuid, NULL::uuid, 'test-model', 'test', 0.10)",
            [TENANT_ID, second_run_id],
        )

        self.assertEqual(decision["decision"], "denied")
        self.assertEqual(decision["budget"]["metric"], "estimated_cost_usd")
        self.assertEqual(float(decision["budget"]["usage"]), 0.25)
        self.assertIn("runtime_budget_exceeded", self._event_types(second_run_id))

    def test_runtime_budget_allows_denies_excess_child_tasks(self) -> None:
        run_id = self._create_run(status="thinking")
        agent_id = self._create_agent("planner")
        parent_id = self._create_task(run_id, agent_id, status="thinking")
        self._create_task(run_id, agent_id, status="completed", parent_task_id=parent_id)
        self._create_task(run_id, agent_id, status="completed", parent_task_id=parent_id)
        self.conn.execute(
            "INSERT INTO agent_runtime_budgets (tenant_id, scope_type, scope_id, max_child_tasks) VALUES (%s, 'task', %s, 2)",
            [TENANT_ID, parent_id],
        )

        decision = self._scalar(
            "SELECT app.runtime_budget_allows(%s::uuid, 'child_tasks', 1, %s::uuid, %s::uuid, %s::uuid, 'test')",
            [TENANT_ID, run_id, parent_id, agent_id],
        )

        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["metric"], "child_tasks")
        self.assertEqual(decision["scope_type"], "task")
        self.assertEqual(decision["usage"], 2)
        self.assertIn("runtime_budget_exceeded", self._event_types(run_id))

    def test_claim_agent_work_filters_runs_by_required_capabilities(self) -> None:
        run_id = self._create_run(status="queued")
        self.conn.execute(
            "UPDATE agent_runs SET required_capabilities = ARRAY['llm:gpt-5','tool:search']::text[] WHERE id = %s",
            [run_id],
        )

        missing = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY['llm:gpt-5']::text[], 5, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )
        matched = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_2', ARRAY['llm:gpt-5','tool:search']::text[], 5, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(missing, [])
        self.assertEqual(len(matched), 1)
        self.assertEqual(str(matched[0]["run_id"]), run_id)

    def test_claim_agent_work_orders_runs_by_priority_then_deadline(self) -> None:
        low = self._create_run(status="queued")
        late_high = self._create_run(status="queued")
        early_high = self._create_run(status="queued")
        self.conn.execute("UPDATE agent_runs SET priority = 1 WHERE id = %s", [low])
        self.conn.execute("UPDATE agent_runs SET priority = 5, deadline_at = now() + interval '2 hours' WHERE id = %s", [late_high])
        self.conn.execute("UPDATE agent_runs SET priority = 5, deadline_at = now() + interval '1 hour' WHERE id = %s", [early_high])

        claimed = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY[]::text[], 1, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(len(claimed), 1)
        self.assertEqual(str(claimed[0]["run_id"]), early_high)

    def test_claim_agent_work_respects_run_not_before(self) -> None:
        run_id = self._create_run(status="queued")
        self.conn.execute("UPDATE agent_runs SET not_before = now() + interval '1 hour' WHERE id = %s", [run_id])

        blocked = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY[]::text[], 1, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )
        self.conn.execute("UPDATE agent_runs SET not_before = now() - interval '1 second' WHERE id = %s", [run_id])
        ready = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY[]::text[], 1, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(blocked, [])
        self.assertEqual(len(ready), 1)
        self.assertEqual(str(ready[0]["run_id"]), run_id)

    def test_claim_agent_work_filters_tasks_by_required_capabilities(self) -> None:
        run_id = self._create_run(status="thinking")
        agent_id = self._create_agent("policy_researcher")
        task_id = self._create_task(run_id, agent_id, status="queued")
        self.conn.execute(
            "UPDATE agent_tasks SET required_capabilities = ARRAY['agent:policy_researcher']::text[], priority = 9 WHERE id = %s",
            [task_id],
        )

        missing = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY['agent:operator']::text[], 5, 60, ARRAY['task']::text[], 'test')",
            [TENANT_ID],
        )
        matched = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_2', ARRAY['agent:policy_researcher']::text[], 5, 60, ARRAY['task']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(missing, [])
        self.assertEqual(len(matched), 1)
        self.assertEqual(str(matched[0]["task_id"]), task_id)
        self.assertEqual(matched[0]["payload"]["priority"], 9)

    def test_task_dependencies_release_parent_only_after_all_required_children_finish(self) -> None:
        run_id = self._create_run(status="thinking")
        agent_id = self._create_agent("planner")
        parent_id = self._create_task(run_id, agent_id, status="waiting_child")
        child_one = self._create_task(run_id, agent_id, status="completed")
        child_two = self._create_task(run_id, agent_id, status="completed")
        self._scalar(
            "SELECT app.create_task_dependency(%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'completion', true, '{}'::jsonb, 'test')",
            [TENANT_ID, run_id, parent_id, child_one],
        )
        self._scalar(
            "SELECT app.create_task_dependency(%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'completion', true, '{}'::jsonb, 'test')",
            [TENANT_ID, run_id, parent_id, child_two],
        )

        first = self._scalar(
            "SELECT app.complete_task_dependencies_for_child(%s::uuid, %s::uuid, %s::uuid, 'completed', 'test')",
            [TENANT_ID, run_id, child_one],
        )
        status_after_first = self._status("agent_tasks", parent_id)
        second = self._scalar(
            "SELECT app.complete_task_dependencies_for_child(%s::uuid, %s::uuid, %s::uuid, 'completed', 'test')",
            [TENANT_ID, run_id, child_two],
        )

        self.assertEqual(first["updated_count"], 1)
        self.assertEqual(first["released_count"], 0)
        self.assertEqual(status_after_first, "waiting_child")
        self.assertEqual(second["updated_count"], 1)
        self.assertEqual(second["released_count"], 1)
        self.assertEqual(self._status("agent_tasks", parent_id), "queued")
        self.assertIn("task_dependency_parent_released", self._event_types(run_id))

    def test_required_task_dependency_failure_blocks_parent(self) -> None:
        run_id = self._create_run(status="thinking")
        agent_id = self._create_agent("planner")
        parent_id = self._create_task(run_id, agent_id, status="waiting_child")
        child_id = self._create_task(run_id, agent_id, status="failed")
        self._scalar(
            "SELECT app.create_task_dependency(%s::uuid, %s::uuid, %s::uuid, %s::uuid, 'completion', true, '{}'::jsonb, 'test')",
            [TENANT_ID, run_id, parent_id, child_id],
        )

        result = self._scalar(
            "SELECT app.complete_task_dependencies_for_child(%s::uuid, %s::uuid, %s::uuid, 'failed', 'test')",
            [TENANT_ID, run_id, child_id],
        )

        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["blocked_count"], 1)
        self.assertEqual(self._status("agent_tasks", parent_id), "blocked")
        self.assertIn("task_dependency_parent_blocked", self._event_types(run_id))

    def test_claim_agent_work_claims_queued_runs_once_and_records_events(self) -> None:
        run_id = self._create_run(status="queued")

        first = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY['llm']::text[], 5, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )
        second = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_2', ARRAY['llm']::text[], 5, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(str(first[0]["run_id"]), run_id)
        self.assertEqual(first[0]["work_type"], "run")
        self.assertEqual(second, [])
        self.assertEqual(self._event_types(run_id), ["work_claimed"])

    def test_claim_agent_work_respects_tenant_concurrency_limit(self) -> None:
        self._create_run(status="queued")
        self._create_run(status="queued")
        self.conn.execute(
            "INSERT INTO agent_runtime_limits (tenant_id, max_concurrent_work) VALUES (%s, 1)",
            [TENANT_ID],
        )

        first = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY[]::text[], 10, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )
        capped = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_2', ARRAY[]::text[], 10, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )
        released = self._scalar(
            "SELECT app.complete_agent_work(%s::uuid, 'worker_1', 'completed', '{}'::jsonb, 'test')",
            [first[0]["work_lease_id"]],
        )
        next_claim = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_2', ARRAY[]::text[], 10, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(capped, [])
        self.assertEqual(released["status"], "completed")
        self.assertEqual(len(next_claim), 1)

    def test_claim_agent_work_claims_tasks_before_runs_and_heartbeat_extends_lease(self) -> None:
        run_id = self._create_run(status="queued")
        agent_id = self._create_agent("refund_operator")
        task_id = self._create_task(run_id, agent_id, status="queued")

        claimed = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY['llm','tool:refund']::text[], 10, 60, ARRAY['task','run']::text[], 'test')",
            [TENANT_ID],
        )
        heartbeat = self._scalar(
            "SELECT app.heartbeat_agent_work(%s::uuid, 'worker_1', 120, 'test')",
            [claimed[0]["work_lease_id"]],
        )

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["work_type"], "task")
        self.assertEqual(str(claimed[0]["task_id"]), task_id)
        self.assertEqual(heartbeat["decision"], "extended")
        self.assertIn("work_heartbeat", self._event_types(run_id))

    def test_expired_work_lease_can_be_reclaimed(self) -> None:
        run_id = self._create_run(status="queued")
        claimed = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_1', ARRAY[]::text[], 1, 5, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )
        self.conn.execute(
            "UPDATE agent_work_leases SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            [claimed[0]["work_lease_id"]],
        )

        reclaimed = self._all(
            "SELECT * FROM app.claim_agent_work(%s::uuid, 'worker_2', ARRAY[]::text[], 1, 60, ARRAY['run']::text[], 'test')",
            [TENANT_ID],
        )

        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(str(reclaimed[0]["run_id"]), run_id)
        self.assertIn("work_lease_expired", self._event_types(run_id))
        self.assertIn("work_claimed", self._event_types(run_id))

    def test_trajectory_view_and_harness_search_are_tenant_scoped(self) -> None:
        run_id = self._create_run(status="thinking")
        memory_id = str(uuid4())
        self.conn.execute(
            "INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor) VALUES (%s, %s, 'policy_found', %s::jsonb, 'test')",
            [TENANT_ID, run_id, '{"policy":"refund window"}'],
        )
        self.conn.execute(
            "INSERT INTO agent_memory (id, tenant_id, memory_type, content, metadata, source_run_id) VALUES (%s, %s, 'case_learning', 'refund window policy memory', '{}'::jsonb, %s)",
            [memory_id, TENANT_ID, run_id],
        )

        trajectory = self._all(
            "SELECT source, step_type, payload FROM app.run_trajectory_v WHERE tenant_id = %s AND run_id = %s ORDER BY sequence_id",
            [TENANT_ID, run_id],
        )
        search = self._all(
            "SELECT source, snippet FROM app.search_harness(%s::uuid, 'refund window', 10)",
            [TENANT_ID],
        )

        self.assertEqual(trajectory[0]["source"], "event")
        self.assertEqual(trajectory[0]["step_type"], "policy_found")
        self.assertEqual({row["source"] for row in search}, {"event", "memory"})

    def _clear_tenant(self) -> None:
        for table in [
            "agent_runtime_budgets",
            "agent_task_dependencies",
            "agent_work_leases",
            "agent_runtime_limits",
            "eval_results",
            "agent_memory",
            "agent_messages",
            "approval_requests",
            "tool_executions",
            "agent_tasks",
            "agents",
            "agent_tool_permissions",
            "agent_events",
            "agent_runs",
            "agent_tools",
            "eval_cases",
        ]:
            self.conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", [TENANT_ID])
        self.conn.commit()

    def _create_run(self, *, status: str = "queued") -> str:
        run_id = str(uuid4())
        self.conn.execute(
            "INSERT INTO agent_runs (id, tenant_id, status, task, model) VALUES (%s, %s, %s, '{}'::jsonb, 'test-model')",
            [run_id, TENANT_ID, status],
        )
        return run_id

    def _create_agent(self, name: str) -> str:
        agent_id = str(uuid4())
        self.conn.execute(
            "INSERT INTO agents (id, tenant_id, name, role, instructions, model) VALUES (%s, %s, %s, 'operator', 'Operate safely.', 'test-model')",
            [agent_id, TENANT_ID, name],
        )
        return agent_id

    def _create_task(self, run_id: str, agent_id: str, *, status: str = "queued", parent_task_id: str | None = None) -> str:
        task_id = str(uuid4())
        self.conn.execute(
            "INSERT INTO agent_tasks (id, tenant_id, run_id, agent_id, parent_task_id, status, input) VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb)",
            [task_id, TENANT_ID, run_id, agent_id, parent_task_id, status],
        )
        return task_id

    def _create_tool(self, name: str, *, input_schema: dict | None = None, requires_approval: bool = False) -> str:
        import json

        tool_id = str(uuid4())
        self.conn.execute(
            "INSERT INTO agent_tools (id, tenant_id, name, input_schema, requires_approval) VALUES (%s, %s, %s, %s::jsonb, %s)",
            [tool_id, TENANT_ID, name, json.dumps(input_schema or {"type": "object"}), requires_approval],
        )
        return tool_id

    def _grant_tenant(self, tool_id: str) -> None:
        self.conn.execute(
            "INSERT INTO agent_tool_permissions (tenant_id, tool_id, subject_type, subject_id, allowed) VALUES (%s, %s, 'tenant', %s, true)",
            [TENANT_ID, tool_id, TENANT_ID],
        )

    def _grant_agent(self, tool_id: str, agent_id: str) -> None:
        self.conn.execute(
            "INSERT INTO agent_tool_permissions (tenant_id, tool_id, subject_type, subject_id, allowed) VALUES (%s, %s, 'agent', %s, true)",
            [TENANT_ID, tool_id, agent_id],
        )

    def _reserve_tool(self, run_id: str, tool_name: str, arguments: dict, *, task_id: str | None = None, agent_id: str | None = None) -> dict:
        import json

        return self._scalar(
            "SELECT app.reserve_tool_execution(%s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb, false, 'test')",
            [TENANT_ID, run_id, task_id, agent_id, tool_name, json.dumps(arguments)],
        )

    def _status(self, table: str, row_id: str) -> str:
        row = self._one(f"SELECT status FROM {table} WHERE id = %s", [row_id])
        return str(row["status"])

    def _event_types(self, run_id: str) -> list[str]:
        return [
            str(row["event_type"])
            for row in self._all(
                "SELECT event_type FROM agent_events WHERE tenant_id = %s AND run_id = %s ORDER BY event_id",
                [TENANT_ID, run_id],
            )
        ]

    def _count(self, table: str, run_id: str) -> int:
        return int(self._one(f"SELECT count(*) AS count FROM {table} WHERE tenant_id = %s AND run_id = %s", [TENANT_ID, run_id])["count"])

    def _scalar(self, sql: str, params: list) -> dict:
        row = self._one(sql, params)
        return next(iter(row.values()))

    def _one(self, sql: str, params: list) -> dict:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def _all(self, sql: str, params: list) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


if __name__ == "__main__":
    unittest.main()
