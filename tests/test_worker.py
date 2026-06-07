from __future__ import annotations

import json
import unittest
from typing import Any, Mapping

from helpers import FakeRepository, SRC, StaticModel  # noqa: F401
from rowplane.tools.base import ToolDefinition
from rowplane.tools.executor import ToolExecutor
from rowplane.tools.registry import ToolRegistry
from rowplane.workers.worker import AgentWorker


class UsageModel(StaticModel):
    def __init__(self, response, *, usage=None, projected_cost_usd=None) -> None:
        super().__init__(response)
        self.last_usage = dict(usage or {})
        if projected_cost_usd is not None:
            self.estimated_call_cost_usd = projected_cost_usd


class WorkerTests(unittest.TestCase):
    def make_worker(self, repo: FakeRepository, response):
        registry = ToolRegistry()
        executor = ToolExecutor(repo, registry)
        return AgentWorker(repo, StaticModel(response), executor)

    def test_run_once_reads_pgmq_message_sets_tenant_and_deletes_message(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        repo.add_queue_message("tenant_1", "run_1", msg_id=42)
        worker = self.make_worker(repo, {"action": "final", "answer": {"ok": True}})

        result = worker.run_once()

        self.assertEqual(result, "completed")
        self.assertEqual(repo.tenant_context, "tenant_1")
        self.assertEqual(repo.deleted_messages, [42])

    def test_run_once_returns_empty_when_queue_has_no_message(self) -> None:
        repo = FakeRepository()
        worker = self.make_worker(repo, {"action": "final", "answer": {}})

        self.assertEqual(worker.run_once(), "empty")

    def test_model_call_budget_denial_blocks_before_model_call(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        repo.add_runtime_budget(scope_type="run", scope_id="run_1", max_model_calls=0)
        model = StaticModel({"action": "final", "answer": {"ok": True}})
        worker = AgentWorker(repo, model, ToolExecutor(repo, ToolRegistry()))

        result = worker.process_run("run_1")

        self.assertEqual(result, "blocked")
        self.assertEqual(model.messages, [])
        self.assertEqual(repo.runs["run_1"]["status"], "blocked")
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("runtime_budget_exceeded", event_types)
        self.assertIn("model_call_denied_by_budget", event_types)
        self.assertIn("run_blocked", event_types)

    def test_model_call_reservation_written_before_allowed_model_call(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        repo.add_runtime_budget(scope_type="run", scope_id="run_1", max_model_calls=1)
        worker = self.make_worker(repo, {"action": "final", "answer": {"ok": True}})

        result = worker.process_run("run_1")

        self.assertEqual(result, "completed")
        self.assertIn("model_call_reserved", [event["event_type"] for event in repo.events])

    def test_model_call_completion_records_usage_metadata(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        model = UsageModel(
            {"action": "final", "answer": {"ok": True}},
            usage={
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
                "estimated_cost_usd": 0.012,
            },
            projected_cost_usd=0.012,
        )
        worker = AgentWorker(repo, model, ToolExecutor(repo, ToolRegistry()))

        result = worker.process_run("run_1")

        self.assertEqual(result, "completed")
        completed = [event for event in repo.events if event["event_type"] == "model_call_completed"]
        self.assertEqual(len(completed), 1)
        payload = completed[0]["payload"]
        self.assertEqual(payload["prompt_tokens"], 11)
        self.assertEqual(payload["completion_tokens"], 7)
        self.assertEqual(payload["total_tokens"], 18)
        self.assertEqual(payload["estimated_cost_usd"], 0.012)
        self.assertIsInstance(payload["latency_ms"], int)

    def test_model_cost_budget_denial_blocks_before_model_call(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        repo.add_runtime_budget(scope_type="tenant", max_estimated_cost_usd=0.01)
        model = UsageModel(
            {"action": "final", "answer": {"ok": True}},
            projected_cost_usd=0.02,
        )
        worker = AgentWorker(repo, model, ToolExecutor(repo, ToolRegistry()))

        result = worker.process_run("run_1")

        self.assertEqual(result, "blocked")
        self.assertEqual(model.messages, [])
        self.assertEqual(repo.runs["run_1"]["status"], "blocked")
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("runtime_budget_exceeded", event_types)
        self.assertIn("model_call_denied_by_budget", event_types)
        self.assertNotIn("model_call_reserved", event_types)

    def test_model_call_failure_records_failed_accounting_event(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        model = UsageModel(RuntimeError("model unavailable"), usage={"estimated_cost_usd": 0.003})
        worker = AgentWorker(repo, model, ToolExecutor(repo, ToolRegistry()))

        result = worker.process_run("run_1")

        self.assertEqual(result, "failed")
        failed = [event for event in repo.events if event["event_type"] == "model_call_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["payload"]["estimated_cost_usd"], 0.003)
        self.assertEqual(failed[0]["payload"]["error"], "model unavailable")

    def test_final_command_completes_run_and_writes_events(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        worker = self.make_worker(repo, {"action": "final", "answer": {"ok": True}})

        result = worker.process_run("run_1")

        self.assertEqual(result, "completed")
        self.assertEqual(repo.runs["run_1"]["status"], "completed")
        self.assertEqual(repo.runs["run_1"]["answer"], {"ok": True})
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("run_status_changed", event_types)
        self.assertIn("run_thinking", event_types)
        self.assertIn("llm_command_received", event_types)
        self.assertIn("run_completed", event_types)


    def test_final_answer_contract_rejects_invalid_answer_and_requeues(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(max_iterations=4)
        run["task"] = {
            "input": "answer with evidence",
            "answer_contract": {
                "schema": {
                    "type": "object",
                    "required": ["decision", "evidence_tools"],
                    "properties": {
                        "decision": {"type": "string"},
                        "evidence_tools": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": False,
                },
                "required_tools": ["search_policy_documents"],
                "must_reference_tools": True,
            },
        }
        worker = self.make_worker(repo, {"action": "final", "answer": {"decision": "approve"}})

        result = worker.process_run("run_1")

        self.assertEqual(result, "queued")
        self.assertEqual(repo.runs["run_1"]["status"], "queued")
        self.assertIn("final_answer_rejected", [event["event_type"] for event in repo.events])
        self.assertEqual(repo.queued, [("tenant_1", "run_1")])

    def test_final_answer_contract_accepts_schema_and_tool_evidence(self) -> None:
        repo = FakeRepository()
        run = repo.add_run()
        run["task"] = {
            "answer_contract": {
                "schema": {"type": "object", "required": ["decision", "evidence_tools"]},
                "required_tools": ["search_policy_documents"],
                "must_reference_tools": True,
            }
        }
        repo.append_event(
            "tenant_1",
            "run_1",
            "tool_completed",
            {"tool_name": "search_policy_documents", "result": {"output": {}}},
        )
        worker = self.make_worker(
            repo,
            {"action": "final", "answer": {"decision": "approve", "evidence_tools": ["search_policy_documents"]}},
        )

        result = worker.process_run("run_1")

        self.assertEqual(result, "completed")
        self.assertEqual(repo.runs["run_1"]["answer"]["decision"], "approve")

    def test_malformed_command_fails_run(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        worker = self.make_worker(repo, {"action": "final", "answer": {}, "extra": "bad"})

        result = worker.process_run("run_1")

        self.assertEqual(result, "failed")
        self.assertEqual(repo.runs["run_1"]["status"], "failed")
        self.assertIn("llm_command_rejected", [event["event_type"] for event in repo.events])

    def test_model_call_failure_fails_run_and_writes_event(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        worker = self.make_worker(repo, RuntimeError("model unavailable"))

        result = worker.process_run("run_1")

        self.assertEqual(result, "failed")
        self.assertEqual(repo.runs["run_1"]["status"], "failed")
        self.assertIn("llm_call_failed", [event["event_type"] for event in repo.events])

    def test_remember_command_records_memory_and_requeues(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        worker = self.make_worker(
            repo,
            {
                "action": "remember",
                "memory_type": "case_learning",
                "content": "Use the short path next time.",
                "metadata": {"case": "a"},
            },
        )

        result = worker.process_run("run_1")

        self.assertEqual(result, "queued")
        self.assertEqual(repo.runs["run_1"]["status"], "queued")
        self.assertEqual(len(repo.memories), 1)
        self.assertEqual(repo.queued, [("tenant_1", "run_1")])


    def test_prompt_includes_registered_tool_contracts(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        repo.add_tool("tenant_1", "lookup_customer_context")
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="lookup_customer_context",
                handler=lambda context, arguments: {"ok": True},
                input_schema={
                    "type": "object",
                    "required": ["customer_id"],
                    "properties": {"customer_id": {"type": "string"}},
                    "additionalProperties": False,
                },
                is_side_effecting=False,
                requires_approval=False,
                description="Look up customer context.",
            )
        )
        model = StaticModel({"action": "final", "answer": {"ok": True}})
        worker = AgentWorker(repo, model, ToolExecutor(repo, registry))

        result = worker.process_run("run_1")

        self.assertEqual(result, "completed")
        state = json.loads(model.messages[0][1]["content"])
        self.assertEqual(state["registered_tools"][0]["name"], "lookup_customer_context")
        self.assertEqual(state["registered_tools"][0]["input_schema"]["required"], ["customer_id"])
        self.assertFalse(state["registered_tools"][0]["requires_approval"])

    def test_tool_validation_failure_requeues_for_model_correction(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        db_tool = repo.add_tool(
            "tenant_1",
            "demo_tool",
            input_schema={
                "type": "object",
                "required": ["x"],
                "properties": {"x": {"type": "integer"}},
                "additionalProperties": False,
            },
        )
        repo.grant_tenant("tenant_1", db_tool["id"])
        registry = ToolRegistry()

        def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            return {"ok": True}

        registry.register(
            ToolDefinition(
                name="demo_tool",
                handler=handler,
                input_schema={
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "integer"}},
                    "additionalProperties": False,
                },
            )
        )
        worker = AgentWorker(
            repo,
            StaticModel({"action": "tool", "tool_name": "demo_tool", "arguments": {"x": 1, "extra": True}}),
            ToolExecutor(repo, registry),
        )

        result = worker.process_run("run_1")

        self.assertEqual(result, "queued")
        self.assertEqual(repo.runs["run_1"]["status"], "queued")
        self.assertEqual(repo.queued, [("tenant_1", "run_1")])
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("tool_validation_failed", event_types)
        self.assertIn("tool_command_correction_requested", event_types)
        self.assertNotIn("run_failed", event_types)

    def test_max_iterations_blocks_before_model_call(self) -> None:
        repo = FakeRepository()
        repo.add_run(iteration_count=2, max_iterations=2)
        model = StaticModel({"action": "final", "answer": {}})
        worker = AgentWorker(repo, model, ToolExecutor(repo, ToolRegistry()))

        result = worker.process_run("run_1")

        self.assertEqual(result, "blocked")
        self.assertEqual(model.messages, [])
        self.assertEqual(repo.runs["run_1"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
