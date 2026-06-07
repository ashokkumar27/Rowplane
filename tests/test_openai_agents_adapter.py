from __future__ import annotations

import builtins
import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for path in (SRC, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from helpers import FakeRepository
from rowplane.adapters import OpenAIAgentsCommandClient
from rowplane.adapters.openai_agents import DEFAULT_OPENAI_AGENTS_MODEL, messages_to_agent_input
from rowplane.tools.base import ToolDefinition
from rowplane.tools.executor import ToolExecutor
from rowplane.tools.registry import ToolRegistry
from rowplane.workers.worker import AgentWorker


class FakeRunner:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def run_sync(self, agent: Any, input: Any, **kwargs: Any) -> Any:
        self.calls.append({"agent": agent, "input": input, "kwargs": kwargs})
        if not self.results:
            raise RuntimeError("no fake result queued")
        return self.results.pop(0)


class FakeResult:
    def __init__(self, final_output: Any, usage: Any | None = None) -> None:
        self.final_output = final_output
        self.raw_responses = [{"usage": usage or {}}]


class FakeUsage:
    input_tokens = 1000
    output_tokens = 250
    total_tokens = 1250


class PydanticLikeCommand:
    def model_dump(self) -> dict[str, Any]:
        return {"action": "final", "answer": {"ok": True}}


class OpenAIAgentsAdapterTests(unittest.TestCase):
    def test_complete_runs_agent_and_returns_string_output(self) -> None:
        agent = object()
        runner = FakeRunner(FakeResult('{"action":"final","answer":{"ok":true}}'))
        adapter = OpenAIAgentsCommandClient(
            agent=agent,
            runner=runner,
            run_config="config",
            max_turns=3,
            runner_options={"context": {"tenant": "tenant_1"}},
        )

        result = adapter.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(json.loads(result), {"action": "final", "answer": {"ok": True}})
        self.assertEqual(len(runner.calls), 1)
        self.assertIs(runner.calls[0]["agent"], agent)
        self.assertIn("USER:\nhello", runner.calls[0]["input"])
        self.assertEqual(
            runner.calls[0]["kwargs"],
            {"context": {"tenant": "tenant_1"}, "run_config": "config", "max_turns": 3},
        )

    def test_complete_serializes_mapping_and_pydantic_like_output(self) -> None:
        runner = FakeRunner(
            FakeResult({"action": "final", "answer": {"ok": True}}),
            FakeResult(PydanticLikeCommand()),
        )
        adapter = OpenAIAgentsCommandClient(agent=object(), runner=runner)

        mapping_result = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))
        pydantic_result = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))

        self.assertEqual(mapping_result, {"action": "final", "answer": {"ok": True}})
        self.assertEqual(pydantic_result, {"action": "final", "answer": {"ok": True}})

    def test_complete_normalizes_common_json_wrappers(self) -> None:
        runner = FakeRunner(
            FakeResult('```json\n{"action":"final","answer":{"ok":true}}\n```'),
            FakeResult('Here is the command:\n{"action":"final","answer":{"ok":true}}'),
        )
        adapter = OpenAIAgentsCommandClient(agent=object(), runner=runner)

        fenced = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))
        wrapped = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))

        self.assertEqual(fenced, {"action": "final", "answer": {"ok": True}})
        self.assertEqual(wrapped, {"action": "final", "answer": {"ok": True}})

    def test_empty_final_output_retries_and_accumulates_usage(self) -> None:
        runner = FakeRunner(
            FakeResult("", FakeUsage()),
            FakeResult({"action": "final", "answer": {"ok": True}}, FakeUsage()),
        )
        adapter = OpenAIAgentsCommandClient(
            agent=object(),
            runner=runner,
            empty_output_retries=1,
            estimated_call_cost_usd=0.01,
            input_cost_per_million=2.0,
            output_cost_per_million=8.0,
        )

        result = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))

        self.assertEqual(result, {"action": "final", "answer": {"ok": True}})
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(adapter.estimated_call_cost_usd, 0.02)
        self.assertEqual(adapter.last_usage["prompt_tokens"], 2000)
        self.assertEqual(adapter.last_usage["completion_tokens"], 500)
        self.assertEqual(adapter.last_usage["total_tokens"], 2500)
        self.assertAlmostEqual(adapter.last_usage["estimated_cost_usd"], 0.008)

    def test_usage_maps_tokens_and_estimates_cost(self) -> None:
        adapter = OpenAIAgentsCommandClient(
            agent=object(),
            runner=FakeRunner(FakeResult('{"action":"final","answer":{}}', FakeUsage())),
            input_cost_per_million=2.0,
            output_cost_per_million=8.0,
        )

        adapter.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(adapter.last_usage["prompt_tokens"], 1000)
        self.assertEqual(adapter.last_usage["completion_tokens"], 250)
        self.assertEqual(adapter.last_usage["total_tokens"], 1250)
        self.assertAlmostEqual(adapter.last_usage["estimated_cost_usd"], 0.004)

    def test_default_client_requires_optional_agents_sdk(self) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "agents":
                raise ImportError("missing agents")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(RuntimeError, "openai-agents"):
                OpenAIAgentsCommandClient()

    def test_default_model_is_lower_cost_mini_model(self) -> None:
        adapter = OpenAIAgentsCommandClient(agent=object(), runner=FakeRunner(FakeResult('{"action":"final","answer":{}}')))

        self.assertEqual(DEFAULT_OPENAI_AGENTS_MODEL, "gpt-5.4-mini")
        self.assertEqual(adapter.model, "gpt-5.4-mini")

    def test_messages_to_agent_input_preserves_roles_and_content(self) -> None:
        text = messages_to_agent_input([
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "{state}"},
        ])

        self.assertIn("SYSTEM:\nReturn JSON.", text)
        self.assertIn("USER:\n{state}", text)

    def test_worker_uses_adapter_output_but_rowplane_executes_tool(self) -> None:
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
        adapter = OpenAIAgentsCommandClient(
            agent=object(),
            runner=FakeRunner(
                FakeResult({"action": "tool", "tool_name": "demo_tool", "arguments": {"x": 7}}),
                FakeResult({"action": "final", "answer": {"ok": True}}),
            ),
        )
        worker = AgentWorker(repo, adapter, ToolExecutor(repo, registry))

        first = worker.process_run("run_1")
        second = worker.process_run("run_1")

        self.assertEqual(first, "completed")
        self.assertEqual(second, "completed")
        self.assertEqual(repo.runs["run_1"]["status"], "completed")
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("tool_completed", event_types)
        self.assertIn("run_completed", event_types)
        self.assertEqual(len(repo.executions), 1)

    def test_invalid_tool_arguments_are_requeued_for_rowplane_correction(self) -> None:
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
                handler=lambda context, arguments: {"ok": True},
                input_schema={
                    "type": "object",
                    "required": ["x"],
                    "properties": {"x": {"type": "integer"}},
                    "additionalProperties": False,
                },
            )
        )
        adapter = OpenAIAgentsCommandClient(
            agent=object(),
            runner=FakeRunner(FakeResult({"action": "tool", "tool_name": "demo_tool", "arguments": {"x": 7, "extra": True}})),
        )
        worker = AgentWorker(repo, adapter, ToolExecutor(repo, registry))

        result = worker.process_run("run_1")

        self.assertEqual(result, "queued")
        self.assertEqual(repo.runs["run_1"]["status"], "queued")
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("tool_validation_failed", event_types)
        self.assertIn("tool_command_correction_requested", event_types)
        self.assertNotIn("run_failed", event_types)


if __name__ == "__main__":
    unittest.main()
