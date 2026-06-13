from __future__ import annotations

import os
import unittest

from helpers import SRC  # noqa: F401

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/rowplane"


class PostgresMigrationTests(unittest.TestCase):
    def test_migration_declares_required_control_plane_invariants(self) -> None:
        from pathlib import Path

        migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "001_init.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS agent_runs", sql)
        self.assertIn("CREATE TABLE IF NOT EXISTS agent_events", sql)
        self.assertIn("FOREIGN KEY (tenant_id, run_id) REFERENCES agent_runs", sql)
        self.assertIn("ALTER TABLE agent_runs FORCE ROW LEVEL SECURITY", sql)
        self.assertIn("trg_agent_runs_log_status_change", sql)
        self.assertIn("idx_approval_requests_one_per_tool_execution", sql)


    def test_management_migration_declares_read_models_and_audit(self) -> None:
        from pathlib import Path

        migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "003_management_views.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS audit_events", sql)
        self.assertIn("CREATE OR REPLACE VIEW management_run_summary_v", sql)
        self.assertIn("CREATE OR REPLACE VIEW management_approval_queue_v", sql)
        self.assertIn("CREATE OR REPLACE VIEW management_tool_health_v", sql)
        self.assertIn("CREATE OR REPLACE VIEW management_eval_summary_v", sql)
        self.assertIn("security_invoker", sql)

    def test_sql_native_runtime_migration_declares_harness_api(self) -> None:
        from pathlib import Path

        migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "004_sql_native_runtime.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("CREATE OR REPLACE FUNCTION app.validate_agent_command", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.reserve_tool_execution", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.complete_tool_execution", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.submit_agent_command", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.resolve_approval_request", sql)
        self.assertIn("CREATE OR REPLACE VIEW app.run_trajectory_v", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.search_harness", sql)


    def test_dynamic_contract_migration_declares_schema_policy_and_search(self) -> None:
        from pathlib import Path

        migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "005_dynamic_contracts.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("ADD COLUMN IF NOT EXISTS output_schema", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.approval_policy_requires_approval", sql)
        self.assertIn("tool_output_validation_failed", sql)
        self.assertIn("idx_agent_memory_fts", sql)


    def test_schema_validator_alignment_migration_declares_extended_subset(self) -> None:
        from pathlib import Path

        migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "006_schema_validator_alignment.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("schema ? 'const'", sql)
        self.assertIn("minLength", sql)
        self.assertIn("schema ? 'items'", sql)
        self.assertIn("minimum", sql)
        self.assertIn("maximum", sql)

    def test_intent_migration_declares_planner_boundary(self) -> None:
        from pathlib import Path

        migration = Path(__file__).resolve().parents[1] / "db" / "migrations" / "013_agent_intents.sql"
        sql = migration.read_text(encoding="utf-8")
        self.assertIn("CREATE OR REPLACE FUNCTION app.validate_agent_intent", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.simulate_agent_intent_policy", sql)
        self.assertIn("CREATE OR REPLACE FUNCTION app.submit_agent_intent", sql)
        self.assertIn("llm_intent_received", sql)
        self.assertIn("intent_decision_recorded", sql)
        self.assertIn("intent_mapped_to_command", sql)

    def test_migration_applies_to_configured_postgres(self) -> None:
        import psycopg

        from rowplane.db.migrations import apply_migrations

        database_url = os.environ.get("ROWPLANE_DATABASE_URL") or os.environ.get("PG_AGENT_DATABASE_URL", DEFAULT_DATABASE_URL)
        with psycopg.connect(database_url, autocommit=False) as conn:
            applied = apply_migrations(conn)
            conn.commit()
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('agent_runs'), to_regclass('agent_events'), to_regclass('audit_events')")
                tables = cur.fetchone()
        self.assertIsNotNone(tables[0])
        self.assertIsNotNone(tables[1])
        self.assertIsNotNone(tables[2])
        self.assertIsInstance(applied, list)


if __name__ == "__main__":
    unittest.main()
