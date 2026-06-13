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
for path in (SRC, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from rowplane.adapters import DeepAgentsIntentClient, LangGraphIntentClient


class FakeGraph:
    def __init__(self, *results: Any) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def invoke(self, input: Any, **kwargs: Any) -> Any:
        self.calls.append({"input": input, "kwargs": kwargs})
        if not self.results:
            raise RuntimeError("no fake graph result")
        return self.results.pop(0)


class FakeAgent(FakeGraph):
    pass


class FakeUsage:
    input_tokens = 100
    output_tokens = 25
    total_tokens = 125


class PydanticLikeIntent:
    def model_dump(self) -> dict[str, Any]:
        return {"schema_version": 1, "intent": "final_answer", "answer": {"ok": True}}


class FrameworkIntentAdapterTests(unittest.TestCase):
    def test_langgraph_adapter_extracts_one_intent_from_messages(self) -> None:
        graph = FakeGraph(
            {
                "messages": [
                    {"role": "assistant", "content": '```json\n{"schema_version":1,"intent":"final_answer","answer":{"ok":true}}\n```'}
                ],
                "usage": FakeUsage(),
            }
        )
        adapter = LangGraphIntentClient(
            graph=graph,
            invoke_config={"thread_id": "run_1"},
            input_cost_per_million=1.0,
            output_cost_per_million=2.0,
        )

        result = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))

        self.assertEqual(result, {"schema_version": 1, "intent": "final_answer", "answer": {"ok": True}})
        self.assertEqual(graph.calls[0]["kwargs"], {"config": {"thread_id": "run_1"}})
        self.assertEqual(adapter.last_usage["prompt_tokens"], 100)
        self.assertEqual(adapter.last_usage["completion_tokens"], 25)

    def test_deepagents_adapter_extracts_structured_response(self) -> None:
        agent = FakeAgent({"structured_response": PydanticLikeIntent()})
        adapter = DeepAgentsIntentClient(agent=agent)

        result = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))

        self.assertEqual(result, {"schema_version": 1, "intent": "final_answer", "answer": {"ok": True}})
        self.assertEqual(agent.calls[0]["input"]["messages"][0][0], "system")

    def test_empty_output_retries(self) -> None:
        graph = FakeGraph({"messages": [{"content": ""}]}, {"messages": [{"content": {"schema_version": 1, "intent": "failure", "reason": "done"}}]})
        adapter = LangGraphIntentClient(graph=graph, empty_output_retries=1)

        result = json.loads(adapter.complete([{"role": "user", "content": "hello"}]))

        self.assertEqual(result["intent"], "failure")
        self.assertEqual(len(graph.calls), 2)

    def test_rejects_framework_native_tool_calls(self) -> None:
        adapter = LangGraphIntentClient(graph=FakeGraph({"messages": [{"content": "", "tool_calls": [{"name": "demo"}]}]}))

        with self.assertRaisesRegex(RuntimeError, "tool calls"):
            adapter.complete([{"role": "user", "content": "hello"}])

    def test_default_langgraph_requires_optional_dependency_or_graph(self) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "langgraph.graph":
                raise ImportError("missing langgraph")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(RuntimeError, "langgraph"):
                LangGraphIntentClient()

    def test_default_deepagents_requires_optional_dependency(self) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "deepagents":
                raise ImportError("missing deepagents")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(RuntimeError, "deepagents"):
                DeepAgentsIntentClient()


if __name__ == "__main__":
    unittest.main()
