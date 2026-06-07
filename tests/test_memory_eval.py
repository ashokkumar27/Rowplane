from __future__ import annotations

import unittest

from helpers import FakeRepository, SRC  # noqa: F401
from rowplane.evals.recorder import EvalRecorder, EvalScores
from rowplane.memory.repository import MemorySearch, build_memory_where, vector_literal


class MemoryAndEvalTests(unittest.TestCase):
    def test_memory_where_combines_tenant_type_and_metadata_filters(self) -> None:
        where_sql, params = build_memory_where(
            MemorySearch(
                tenant_id="tenant_1",
                memory_type="case_learning",
                metadata_contains={"policy": "x"},
            )
        )

        self.assertIn("tenant_id = %s", where_sql)
        self.assertIn("memory_type = %s", where_sql)
        self.assertIn("metadata @> %s::jsonb", where_sql)
        self.assertEqual(params, ["tenant_1", "case_learning", {"policy": "x"}])

    def test_memory_search_requires_tenant_and_bounded_limit(self) -> None:
        with self.assertRaises(ValueError):
            MemorySearch(tenant_id="", limit=10)
        with self.assertRaises(ValueError):
            MemorySearch(tenant_id="tenant_1", limit=101)

    def test_vector_literal(self) -> None:
        self.assertEqual(vector_literal([1, 2.5]), "[1.0,2.5]")

    def test_eval_recorder_creates_result_and_event(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        result = EvalRecorder(repo).record(
            "tenant_1",
            "case_1",
            "run_1",
            EvalScores(correctness=1.0, tool_correctness=0.5),
        )

        self.assertEqual(result["scores"]["correctness"], 1.0)
        self.assertEqual(repo.events[-1]["event_type"], "eval_result_created")


if __name__ == "__main__":
    unittest.main()
