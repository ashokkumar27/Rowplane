from __future__ import annotations

import unittest

from helpers import SRC  # noqa: F401
from rowplane.samples import run_sample_suite


class SampleUseCaseTests(unittest.TestCase):
    def test_sample_suite_exercises_expected_harness_paths(self) -> None:
        result = run_sample_suite()
        scenarios = {scenario.name: scenario for scenario in result.scenarios}

        self.assertEqual(set(scenarios), {
            "policy_retrieval_qa",
            "refund_approval",
            "case_learning_memory",
            "permission_denied_safety",
        })
        self.assertEqual(scenarios["policy_retrieval_qa"].final_status, "completed")
        self.assertIn("model_call_reserved", scenarios["policy_retrieval_qa"].event_types)
        self.assertIn("model_call_completed", scenarios["policy_retrieval_qa"].event_types)
        self.assertIn("tool_completed", scenarios["policy_retrieval_qa"].event_types)
        self.assertEqual(scenarios["refund_approval"].final_status, "completed")
        self.assertIn("model_call_reserved", scenarios["refund_approval"].event_types)
        self.assertIn("model_call_completed", scenarios["refund_approval"].event_types)
        self.assertIn("approval_requested", scenarios["refund_approval"].event_types)
        self.assertIn("approval_resolved", scenarios["refund_approval"].event_types)
        self.assertEqual(scenarios["case_learning_memory"].final_status, "completed")
        self.assertIn("memory_recorded", scenarios["case_learning_memory"].event_types)
        self.assertEqual(scenarios["permission_denied_safety"].final_status, "failed")
        self.assertIn("tool_permission_denied", scenarios["permission_denied_safety"].event_types)
        self.assertNotIn("tool_started", scenarios["permission_denied_safety"].event_types)

    def test_sample_suite_records_eval_scores_for_each_scenario(self) -> None:
        result = run_sample_suite()

        self.assertEqual(result.harness_assessment["sample_pass_rate"], 1.0)
        for scenario in result.scenarios:
            self.assertEqual(scenario.scores["correctness"], 1.0)
            self.assertIn("eval_result_created", scenario.event_types)


if __name__ == "__main__":
    unittest.main()
