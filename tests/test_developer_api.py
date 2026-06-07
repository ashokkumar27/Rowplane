from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

from helpers import SRC  # noqa: F401

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/rowplane"
TENANT_ID = "00000000-0000-0000-0000-000000000654"


class RowplaneImportCompatibilityTests(unittest.TestCase):
    def test_import_rowplane_does_not_import_management_api(self) -> None:
        script = (
            "import sys; import rowplane; "
            "print('pg_agent.management.api' in sys.modules); "
            "print('rowplane.management.api' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.splitlines(), ["False", "False"])

    def test_rowplane_and_legacy_pg_agent_imports_are_supported(self) -> None:
        from rowplane import AgentHarness as RowplaneHarness, tool as rowplane_tool
        from rowplane.adapters import OpenAIAgentsCommandClient as RowplaneOpenAIAgentsCommandClient
        from rowplane.adapters import OpenAIModelClient as RowplaneOpenAIModelClient
        from rowplane.runtime.errors import MalformedCommand as RowplaneMalformedCommand
        from pg_agent import AgentHarness as LegacyHarness, tool as legacy_tool
        from pg_agent.adapters import OpenAIAgentsCommandClient as LegacyOpenAIAgentsCommandClient
        from pg_agent.adapters import OpenAIModelClient as LegacyOpenAIModelClient
        from pg_agent.runtime.errors import MalformedCommand as LegacyMalformedCommand

        self.assertIs(RowplaneHarness, LegacyHarness)
        self.assertIs(rowplane_tool, legacy_tool)
        self.assertIs(RowplaneOpenAIModelClient, LegacyOpenAIModelClient)
        self.assertIs(RowplaneOpenAIAgentsCommandClient, LegacyOpenAIAgentsCommandClient)
        self.assertIs(RowplaneMalformedCommand, LegacyMalformedCommand)

    def test_legacy_cli_help_uses_legacy_program_name(self) -> None:
        from rowplane.cli import build_parser

        self.assertIn("usage: rowplane", build_parser().format_help())
        self.assertIn("usage: pg-agent", build_parser(prog="pg-agent").format_help())


class DeveloperApiTests(unittest.TestCase):
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

    def test_tool_decorator_accepts_pydantic_schema(self) -> None:
        from pydantic import BaseModel

        from rowplane import tool
        from rowplane.client import as_tool_definition

        class SearchInput(BaseModel):
            query: str
            top_k: int

        @tool(input_schema=SearchInput, description="Search policy documents.")
        def search_policy_documents(ctx, args):
            return {"documents": []}

        definition = as_tool_definition(search_policy_documents)

        self.assertEqual(definition.name, "search_policy_documents")
        self.assertEqual(definition.input_schema["type"], "object")
        self.assertIn("query", definition.input_schema["properties"])
        self.assertEqual(definition.description, "Search policy documents.")

    def test_harness_registers_tool_runs_worker_and_explains_from_postgres(self) -> None:
        from rowplane import AgentHarness, tool
        from rowplane.samples.use_cases import ScriptedModel

        @tool(
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            },
            description="Search policy documents.",
        )
        def search_policy_documents(ctx, args):
            return {"documents": [{"id": "policy:dpa", "title": "DPA"}]}

        model = ScriptedModel([
            {"action": "tool", "tool_name": "search_policy_documents", "arguments": {"query": "dpa"}},
            {"action": "final", "answer": {"answer": "Use the DPA.", "citations": ["policy:dpa"]}},
        ])

        with AgentHarness(self.database_url, tenant_id=TENANT_ID, model_client=model) as harness:
            tool_row = harness.register_tool(search_policy_documents)
            run = harness.run({"question": "Which data processing policy applies?"})
            run_status = run.status
            run_answer = run.answer
            explanation = run.explain()
            events = run.events()
            executions = run.tool_executions()

        self.assertEqual(run_status, "completed")
        self.assertEqual(run_answer, {"answer": "Use the DPA.", "citations": ["policy:dpa"]})
        self.assertEqual(explanation["status"], "completed")
        self.assertEqual(explanation["requested_tools"], ["search_policy_documents"])
        self.assertIn("tool_completed", [event["event_type"] for event in events])
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["tool_name"], "search_policy_documents")
        self.assertFalse(executions[0]["tool_is_side_effecting"])
        self.assertEqual(self._count("agent_tools"), 1)
        self.assertEqual(self._count("agent_tool_permissions"), 1)
        self.assertEqual(str(tool_row["name"]), "search_policy_documents")

    def test_harness_set_budget_upserts_tenant_budget(self) -> None:
        from rowplane import AgentHarness

        with AgentHarness(self.database_url, tenant_id=TENANT_ID) as harness:
            first = harness.set_budget(
                max_model_calls=10,
                max_tool_executions=5,
                max_estimated_cost_usd=1.25,
                max_active_work=2,
            )
            second = harness.set_budget(
                max_model_calls=20,
                max_tool_executions=8,
                max_child_tasks=3,
                max_estimated_cost_usd=2.5,
                metadata={"tier": "starter"},
            )
            loaded = harness.get_budget()

        self.assertEqual(first["scope_type"], "tenant")
        self.assertEqual(str(first["scope_id"]), TENANT_ID)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(loaded["max_model_calls"], 20)
        self.assertEqual(loaded["max_tool_executions"], 8)
        self.assertEqual(loaded["max_child_tasks"], 3)
        self.assertEqual(float(loaded["max_estimated_cost_usd"]), 2.5)
        self.assertEqual(loaded["metadata"], {"tier": "starter"})
        self.assertEqual(self._count("agent_runtime_budgets"), 1)

    def test_cli_set_budget(self) -> None:
        output = self._run_cli([
            "--database-url", self.database_url,
            "set-budget",
            "--tenant-id", TENANT_ID,
            "--max-model-calls", "100",
            "--max-tool-executions", "40",
            "--max-active-work", "4",
            "--max-estimated-cost-usd", "12.5",
            "--metadata-json", '{"owner":"platform"}',
        ])

        self.assertEqual(output["scope_type"], "tenant")
        self.assertEqual(output["scope_id"], TENANT_ID)
        self.assertEqual(output["max_model_calls"], 100)
        self.assertEqual(output["max_tool_executions"], 40)
        self.assertEqual(output["max_active_work"], 4)
        self.assertEqual(float(output["max_estimated_cost_usd"]), 12.5)
        self.assertEqual(output["metadata"], {"owner": "platform"})
        self.assertEqual(self._count("agent_runtime_budgets"), 1)

    def test_harness_drain_leased_work_uses_sql_scheduler(self) -> None:
        from rowplane import AgentHarness, tool
        from rowplane.samples.use_cases import ScriptedModel

        @tool(
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
                "additionalProperties": False,
            }
        )
        def search_policy_documents(ctx, args):
            return {"documents": [{"id": "policy:scheduler"}]}

        model = ScriptedModel([
            {"action": "tool", "tool_name": "search_policy_documents", "arguments": {"query": "scheduler"}},
            {"action": "final", "answer": {"answer": "scheduled", "citations": ["policy:scheduler"]}},
        ])

        with AgentHarness(self.database_url, tenant_id=TENANT_ID, model_client=model) as harness:
            harness.register_tool(search_policy_documents)
            run = harness.create_run({"question": "Use leased work"})
            outcomes = harness.drain_leased_work(worker_id="test_worker", max_steps=5, kinds=["run"])
            run_status = run.status
            run_answer = run.answer
            events = run.events()

        event_types = [event["event_type"] for event in events]
        self.assertEqual(run_status, "completed")
        self.assertEqual(run_answer, {"answer": "scheduled", "citations": ["policy:scheduler"]})
        self.assertEqual(outcomes, ["completed", "completed", "empty"])
        self.assertIn("work_claimed", event_types)
        self.assertIn("work_lease_completed", event_types)

    def test_run_until_terminal_resolves_multiple_approval_cycles(self) -> None:
        from rowplane import AgentHarness, tool
        from rowplane.samples.use_cases import ScriptedModel

        approval_schema = {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        }

        @tool(name="first_risky_tool", input_schema=approval_schema, is_side_effecting=True, requires_approval=True)
        def first_risky_tool(ctx, args):
            return {"status": "first_done", "value": args["value"]}

        @tool(name="second_risky_tool", input_schema=approval_schema, is_side_effecting=True, requires_approval=True)
        def second_risky_tool(ctx, args):
            return {"status": "second_done", "value": args["value"]}

        model = ScriptedModel([
            {"action": "tool", "tool_name": "first_risky_tool", "arguments": {"value": "one"}},
            {"action": "tool", "tool_name": "second_risky_tool", "arguments": {"value": "two"}},
            {"action": "final", "answer": {"status": "completed_after_two_approvals"}},
        ])

        approved: list[str] = []
        with AgentHarness(self.database_url, tenant_id=TENANT_ID, model_client=model) as harness:
            harness.register_tool(first_risky_tool)
            harness.register_tool(second_risky_tool)
            run = harness.create_run({"request": "run two risky tools"})
            outcomes = harness.run_until_terminal(
                run.run_id,
                approval_handler=lambda approval: approved.append(str(approval["id"])) is None or True,
                resolved_by="test_approver",
            )
            run_status = run.status
            run_answer = run.answer
            approvals = run.approvals()
            executions = run.tool_executions()

        self.assertEqual(run_status, "completed")
        self.assertEqual(run_answer, {"status": "completed_after_two_approvals"})
        self.assertGreaterEqual(outcomes.count("waiting_approval"), 2)
        self.assertEqual(len(approved), 2)
        self.assertEqual([approval["status"] for approval in approvals], ["approved", "approved"])
        self.assertEqual([execution["tool_name"] for execution in executions], ["first_risky_tool", "second_risky_tool"])


    def test_harness_replay_and_memory_search_record_evidence(self) -> None:
        from rowplane import AgentHarness

        with AgentHarness(self.database_url, tenant_id=TENANT_ID) as harness:
            run = harness.create_run({"request": "remember case"}, queue=False)
            harness.repo.create_memory(
                TENANT_ID,
                "case_learning",
                "Refunds above 100 dollars require finance approval.",
                {"domain": "refunds"},
                source_run_id=run.run_id,
            )
            harness.conn.commit()
            rows = harness.search_memory(
                query="finance approval",
                memory_type="case_learning",
                metadata_contains={"domain": "refunds"},
                record_event_for_run_id=run.run_id,
            )
            replay = harness.replay(run.run_id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["memory_type"], "case_learning")
        self.assertEqual(len(replay["memory"]), 1)
        self.assertIn("memory_search_performed", [event["event_type"] for event in replay["events"]])

    def test_cli_register_tool_create_run_and_explain(self) -> None:
        from rowplane.cli import main

        register_output = self._run_cli([
            "--database-url", self.database_url,
            "register-tool",
            "--tenant-id", TENANT_ID,
            "--name", "create_support_ticket",
            "--description", "Create a support ticket.",
            "--schema-json", '{"type":"object","required":["title"],"properties":{"title":{"type":"string"}},"additionalProperties":false}',
            "--output-schema-json", '{"type":"object","required":["ticket_id"],"properties":{"ticket_id":{"type":"string"}}}',
            "--approval-policy-json", '{"rules":[{"field":"priority","operator":"eq","value":"high"}]}',
        ])
        self.assertEqual(register_output["name"], "create_support_ticket")
        self.assertEqual(register_output["output_schema"]["required"], ["ticket_id"])
        self.assertEqual(register_output["approval_policy"]["rules"][0]["field"], "priority")
        self.assertTrue(register_output["enabled"])

        run_output = self._run_cli([
            "--database-url", self.database_url,
            "run",
            "--tenant-id", TENANT_ID,
            "--task-json", '{"request":"Open a support ticket"}',
            "--model", "cli-test-model",
        ])
        self.assertEqual(run_output["status"], "queued")
        self.assertEqual(run_output["model"], "cli-test-model")

        explain_output = self._run_cli([
            "--database-url", self.database_url,
            "explain",
            "--tenant-id", TENANT_ID,
            run_output["id"],
        ])
        self.assertEqual(explain_output["status"], "queued")
        self.assertEqual(explain_output["run_id"], run_output["id"])
        self.assertEqual(self._count("agent_runs"), 1)
        self.assertEqual(self._count("agent_tools"), 1)
        self.assertEqual(self._count("agent_tool_permissions"), 1)

    def _run_cli(self, argv: list[str]) -> dict:
        from rowplane.cli import main

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exit_code = main(argv)
        self.assertEqual(exit_code, 0)
        loaded = json.loads(stream.getvalue())
        self.assertIsInstance(loaded, dict)
        return loaded

    def _clear_tenant(self) -> None:
        self.conn.execute(
            """
            TRUNCATE TABLE
              agent_runtime_budgets,
              agent_task_dependencies,
              agent_work_leases,
              agent_runtime_limits,
              eval_results,
              agent_memory,
              agent_messages,
              approval_requests,
              tool_executions,
              agent_tasks,
              agents,
              agent_tool_permissions,
              agent_events,
              agent_runs,
              agent_tools,
              eval_cases
            RESTART IDENTITY CASCADE
            """
        )
        self.conn.commit()

    def _count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT count(*) AS count FROM {table} WHERE tenant_id = %s", [TENANT_ID]).fetchone()
        return int(row["count"])


if __name__ == "__main__":
    unittest.main()
