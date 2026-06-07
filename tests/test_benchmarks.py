from __future__ import annotations

import json
import unittest

from helpers import ROOT, SRC  # noqa: F401

from benchmarks.adapters import build_adapters, build_experimental_framework_adapters
from benchmarks.report import render_report
from benchmarks.scenarios import build_scenarios
from benchmarks.scoring import aggregate_scores, score_run
from benchmarks.toolbox import BenchmarkToolbox
from benchmarks.types import BenchmarkRunRecord


class BenchmarkTests(unittest.TestCase):
    def test_scenarios_cover_planned_usefulness_cases(self) -> None:
        scenarios = {scenario.name: scenario for scenario in build_scenarios()}

        self.assertEqual(
            set(scenarios),
            {
                "policy_retrieval_qa",
                "refund_approval",
                "permission_denied_safety",
                "tenant_memory_search",
                "multi_agent_refund_review",
            },
        )
        self.assertIn("approval", scenarios["refund_approval"].tags)
        self.assertIn("tenant_isolation", scenarios["tenant_memory_search"].tags)
        self.assertIn("multi_agent", scenarios["multi_agent_refund_review"].tags)

    def test_toolbox_blocks_side_effect_until_approval(self) -> None:
        record = BenchmarkRunRecord("framework", "refund_approval", 1, "model")
        toolbox = BenchmarkToolbox(record)

        blocked = toolbox.issue_refund("cust_123", 2500, "duplicate")
        approved = toolbox.request_approval("duplicate refund")
        issued = toolbox.issue_refund("cust_123", 2500, "duplicate")

        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(issued["status"], "refund_issued")
        self.assertEqual(len(record.side_effects), 1)

    def test_scoring_rewards_sql_evidence_and_approval_order(self) -> None:
        scenario = next(item for item in build_scenarios() if item.name == "refund_approval")
        record = BenchmarkRunRecord(
            framework="rowplane",
            scenario=scenario.name,
            repeat=1,
            model="gpt-5.4-mini",
            answer={"status": "refund_issued"},
            approvals=[{"status": "approved"}],
            side_effects=[{"status": "refund_issued"}],
            sql_evidence=[{"source": "agent_events"}],
            trace_events=[
                {"tool_name": "request_approval", "result": {"status": "approved"}},
                {"tool_name": "issue_refund", "result": {"status": "refund_issued"}},
            ],
        )
        record.score = score_run(record, scenario)

        self.assertGreaterEqual(record.score["governance_safety"], 20)
        self.assertGreaterEqual(record.score["auditability_sql_evidence"], 12)
        self.assertEqual(record.score["task_success"], record.score["functional_correctness"])
        self.assertEqual(
            record.score["harness_control_plane"],
            record.score["governance_safety"] + record.score["auditability_sql_evidence"],
        )
        self.assertTrue(record.score["passed"])

    def test_report_stub_does_not_claim_live_results(self) -> None:
        report = render_report([], model="gpt-5.4-mini")

        self.assertIn("No live benchmark records", report)
        self.assertIn("OPENAI_API_KEY", report)
        self.assertIn("Framework Positioning", report)
        self.assertIn("control plane", report)
        self.assertIn("plain_openai_tool_loop", report)
        self.assertIn("AgentBench", report)

    def test_adapter_registry_defaults_to_legitimate_baseline(self) -> None:
        adapters = build_adapters(database_url="postgresql://example")
        self.assertEqual([adapter.name for adapter in adapters], ["rowplane", "plain_openai_tool_loop"])

        experimental = build_experimental_framework_adapters()
        self.assertEqual(
            [adapter.name for adapter in experimental],
            [
                "langgraph",
                "langchain",
                "crewai",
                "pydantic_ai",
                "openai_agents",
                "llamaindex",
            ],
        )

        record = BenchmarkRunRecord("rowplane", "policy_retrieval_qa", 1, "gpt-5.4-mini")
        payload = json.loads(json.dumps(record.to_dict()))
        self.assertEqual(payload["framework"], "rowplane")
        self.assertIn("tool_calls", payload)

    def test_benchmark_assets_are_linked_from_readme(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("benchmarks/README.md", readme)
        self.assertIn("benchmarks/reports/usefulness_benchmark.md", readme)
        self.assertTrue((ROOT / "benchmarks" / "requirements.txt").exists())
        self.assertTrue((ROOT / "benchmarks" / "requirements-frameworks.txt").exists())
        self.assertEqual(
            (ROOT / "benchmarks" / "requirements.txt").read_text(encoding="utf-8").strip(),
            "openai>=1.100",
        )


if __name__ == "__main__":
    unittest.main()
