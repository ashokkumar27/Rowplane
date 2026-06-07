from __future__ import annotations

import os
import unittest

from helpers import SRC  # noqa: F401

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/rowplane"


class PostgresRealUseCasesTests(unittest.TestCase):
    def test_real_postgres_sample_suite(self) -> None:
        import psycopg  # noqa: F401

        from examples.postgres_showcase import run_postgres_sample_suite

        database_url = os.environ.get("ROWPLANE_DATABASE_URL") or os.environ.get("PG_AGENT_DATABASE_URL", DEFAULT_DATABASE_URL)
        result = run_postgres_sample_suite(database_url, reset=True)

        expected_names = {
            "policy_retrieval_qa",
            "refund_approval",
            "case_learning_memory",
            "permission_denied_safety",
            "multi_agent_refund_review",
            "sql_schema_guardrail",
            "sre_rollback_approval",
            "enterprise_state_diff_ticket",
            "customer_support_resolution",
            "tenant_boundary_search_isolation",
            "trajectory_replay_debug",
            "final_answer_contract",
        }
        scenarios = {scenario.name: scenario for scenario in result.scenarios}

        self.assertEqual(set(scenarios), expected_names)
        self.assertEqual(result.harness_assessment["sample_pass_rate"], 1.0)
        self.assertGreaterEqual(result.harness_assessment["model_accounting_coverage"], 0.8)
        self.assertEqual({scenario.final_status for scenario in result.scenarios}, {"completed", "failed", "blocked"})
        self.assertIn("sql_runtime_api", result.capability_matrix["sql_schema_guardrail"])
        self.assertIn("developer_api", result.capability_matrix["policy_retrieval_qa"])
        self.assertIn("developer_api", result.capability_matrix["refund_approval"])
        self.assertIn("global_budget", result.capability_matrix["refund_approval"])
        self.assertIn("model_accounting", result.capability_matrix["refund_approval"])
        self.assertIn("developer_api", result.capability_matrix["enterprise_state_diff_ticket"])
        self.assertIn("state_diff_eval", result.capability_matrix["enterprise_state_diff_ticket"])
        self.assertIn("leased_workers", result.capability_matrix["customer_support_resolution"])
        self.assertIn("support_workflow", result.capability_matrix["customer_support_resolution"])
        self.assertIn("trajectory_replay", result.capability_matrix["trajectory_replay_debug"])
        self.assertIn("answer_contract", result.capability_matrix["final_answer_contract"])

        self.assertTrue(any("AgentHarness" in note for note in scenarios["policy_retrieval_qa"].notes))
        self.assertTrue(any("harness.approve" in note for note in scenarios["refund_approval"].notes))
        self.assertIn("tool_validation_failed", scenarios["sql_schema_guardrail"].event_types)
        self.assertIn("approval_requested", scenarios["sre_rollback_approval"].event_types)
        self.assertIn("tool_completed", scenarios["enterprise_state_diff_ticket"].event_types)
        self.assertIn("work_claimed", scenarios["customer_support_resolution"].event_types)
        self.assertIn("approval_requested", scenarios["customer_support_resolution"].event_types)
        self.assertIn("memory_recorded", scenarios["customer_support_resolution"].event_types)
        self.assertEqual(scenarios["customer_support_resolution"].scores["policy_compliance"], 1.0)
        self.assertEqual(scenarios["tenant_boundary_search_isolation"].scores["policy_compliance"], 1.0)
        self.assertEqual(scenarios["trajectory_replay_debug"].final_status, "blocked")
        self.assertIn("final_answer_rejected", scenarios["final_answer_contract"].event_types)
        model_accounted = {
            "policy_retrieval_qa",
            "refund_approval",
            "case_learning_memory",
            "permission_denied_safety",
            "multi_agent_refund_review",
            "sre_rollback_approval",
            "enterprise_state_diff_ticket",
            "customer_support_resolution",
            "trajectory_replay_debug",
            "final_answer_contract",
        }
        for name in model_accounted:
            self.assertIn("model_call_reserved", scenarios[name].event_types)
            self.assertIn("model_call_completed", scenarios[name].event_types)
            self.assertEqual(scenarios[name].scores.get("model_accounting"), 1.0)
        for scenario in result.scenarios:
            self.assertIn("eval_result_created", scenario.event_types)
            self.assertEqual(scenario.scores["correctness"], 1.0)


if __name__ == "__main__":
    unittest.main()
