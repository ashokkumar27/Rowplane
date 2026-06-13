from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from helpers import FakeRepository
from rowplane.runtime.errors import MalformedCommand
from rowplane.runtime.intents import (
    _intent_to_command,
    intent_to_event_payload,
    normalize_intent,
    parse_intent,
)
from rowplane.tools.base import ToolDefinition
from rowplane.tools.executor import ToolExecutor
from rowplane.tools.registry import ToolRegistry
from rowplane.workers.worker import AgentWorker


class IntentRuntimeTests(unittest.TestCase):
    def test_parse_tool_request_intent_and_map_privately(self) -> None:
        intent = parse_intent(
            {
                "schema_version": 1,
                "intent_id": "intent-1",
                "intent": "tool_request",
                "tool_name": "search_policy_documents",
                "arguments": {"query": "refund"},
            }
        )

        self.assertEqual(normalize_intent(intent)["intent"], "tool_request")
        command = _intent_to_command(intent)
        self.assertEqual(command.action, "tool")
        self.assertEqual(command.tool_name, "search_policy_documents")

    def test_rejects_multiple_intents_and_native_tool_calls(self) -> None:
        with self.assertRaisesRegex(MalformedCommand, "exactly one intent"):
            parse_intent('[{"schema_version":1,"intent":"failure","reason":"x"}]')

        with self.assertRaisesRegex(MalformedCommand, "tool calls"):
            parse_intent({"schema_version": 1, "intent": "tool_request", "tool_calls": []})

    def test_rejects_human_input_and_adapter_approval_decisions(self) -> None:
        with self.assertRaisesRegex(MalformedCommand, "approval"):
            parse_intent({"schema_version": 1, "intent": "human_input", "reason": "x", "payload": {}})

        with self.assertRaisesRegex(MalformedCommand, "approval"):
            parse_intent(
                {
                    "schema_version": 1,
                    "intent": "clarification_request",
                    "reason": "Approval required.",
                    "payload": {},
                }
            )

    def test_intent_event_payload_redacts_rich_fields(self) -> None:
        intent = parse_intent(
            {
                "schema_version": 1,
                "intent": "tool_request",
                "tool_name": "demo_tool",
                "arguments": {"api_key": "secret"},
            }
        )

        payload = intent_to_event_payload(intent)

        self.assertEqual(payload["intent"], "tool_request")
        self.assertEqual(payload["tool_name"], "demo_tool")
        self.assertNotIn("arguments", payload)

    def test_worker_records_intent_decision_then_rowplane_executes_tool(self) -> None:
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
        registry.register(
            ToolDefinition(
                name="demo_tool",
                handler=lambda context, arguments: {"ok": arguments["x"]},
                input_schema={
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "integer"}},
                    "additionalProperties": False,
                },
            )
        )
        model = ScriptedModel(
            {
                "schema_version": 1,
                "intent": "tool_request",
                "tool_name": "demo_tool",
                "arguments": {"x": 7},
            },
            {"schema_version": 1, "intent": "final_answer", "answer": {"ok": True}},
        )
        worker = AgentWorker(repo, model, ToolExecutor(repo, registry))

        first = worker.process_run("run_1")
        second = worker.process_run("run_1")

        self.assertEqual(first, "completed")
        self.assertEqual(second, "completed")
        self.assertEqual(repo.runs["run_1"]["status"], "completed")
        event_types = [event["event_type"] for event in repo.events]
        self.assertLess(event_types.index("llm_intent_received"), event_types.index("intent_mapped_to_command"))
        self.assertIn("intent_decision_recorded", event_types)
        self.assertIn("tool_completed", event_types)
        self.assertEqual(len(repo.executions), 1)

    def test_worker_records_requires_approval_before_approval_request(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        db_tool = repo.add_tool("tenant_1", "demo_tool", requires_approval=True)
        repo.grant_tenant("tenant_1", db_tool["id"])
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="demo_tool",
                handler=lambda context, arguments: {"should_not_run_without_approval": True},
            )
        )
        model = ScriptedModel(
            {
                "schema_version": 1,
                "intent": "tool_request",
                "tool_name": "demo_tool",
                "arguments": {},
            }
        )
        worker = AgentWorker(repo, model, ToolExecutor(repo, registry))

        outcome = worker.process_run("run_1")

        self.assertEqual(outcome, "waiting_approval")
        event_types = [event["event_type"] for event in repo.events]
        decision_index = event_types.index("intent_decision_recorded")
        approval_index = event_types.index("approval_requested")
        self.assertLess(decision_index, approval_index)
        decisions = [event["payload"] for event in repo.events if event["event_type"] == "intent_decision_recorded"]
        self.assertEqual(decisions[-1]["decision"], "requires_approval")
        self.assertEqual(len(repo.approvals), 1)

    def test_policy_simulation_reports_idempotent_replay(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        db_tool = repo.add_tool("tenant_1", "demo_tool")
        repo.grant_tenant("tenant_1", db_tool["id"])
        registry = ToolRegistry()
        registry.register(ToolDefinition(name="demo_tool", handler=lambda context, arguments: {"ok": True}))
        model = ScriptedModel(
            {"schema_version": 1, "intent": "tool_request", "tool_name": "demo_tool", "arguments": {}},
            {"schema_version": 1, "intent": "tool_request", "tool_name": "demo_tool", "arguments": {}},
        )
        worker = AgentWorker(repo, model, ToolExecutor(repo, registry))

        self.assertEqual(worker.process_run("run_1"), "completed")
        self.assertEqual(worker.process_run("run_1"), "completed")

        decisions = [event["payload"] for event in repo.events if event["event_type"] == "intent_decision_recorded"]
        self.assertEqual(decisions[-1]["decision"], "idempotent_replay")
        self.assertEqual(len(repo.executions), 1)


class ScriptedModel:
    def __init__(self, *outputs: dict[str, Any]) -> None:
        self.outputs = list(outputs)

    def complete(self, messages):
        if not self.outputs:
            raise RuntimeError("no scripted output")
        return json.dumps(self.outputs.pop(0))


if __name__ == "__main__":
    unittest.main()
