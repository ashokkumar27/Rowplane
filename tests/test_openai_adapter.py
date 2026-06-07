from __future__ import annotations

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

from rowplane.adapters.openai import OpenAIModelClient
from rowplane.tools.executor import ToolExecutor
from rowplane.tools.registry import ToolRegistry
from rowplane.workers.worker import AgentWorker

from helpers import FakeRepository


class FakeResponses:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response: Any) -> None:
        self.responses = FakeResponses(response)


class ObjectResponse:
    output_text = '{"action":"final","answer":{"ok":true}}'

    class Usage:
        input_tokens = 1000
        output_tokens = 200
        total_tokens = 1200

    usage = Usage()


class OpenAIAdapterTests(unittest.TestCase):
    def test_complete_calls_responses_create_and_returns_output_text(self) -> None:
        client = FakeOpenAIClient(ObjectResponse())
        adapter = OpenAIModelClient(
            client=client,
            model="gpt-test",
            instructions="Return one JSON command.",
            max_output_tokens=128,
            request_options={"metadata": {"tenant": "tenant_1"}},
        )

        result = adapter.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(result, '{"action":"final","answer":{"ok":true}}')
        self.assertEqual(len(client.responses.calls), 1)
        request = client.responses.calls[0]
        self.assertEqual(request["model"], "gpt-test")
        self.assertEqual(request["instructions"], "Return one JSON command.")
        self.assertEqual(request["max_output_tokens"], 128)
        self.assertEqual(request["metadata"], {"tenant": "tenant_1"})
        self.assertEqual(request["input"], [{"role": "user", "content": "hello"}])

    def test_complete_extracts_nested_output_text_when_output_text_missing(self) -> None:
        response = {
            "output": [
                {"content": [{"type": "output_text", "text": '{"action":"final",'}]},
                {"content": [{"type": "output_text", "text": '"answer":{}}'}]},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        adapter = OpenAIModelClient(client=FakeOpenAIClient(response))

        result = adapter.complete([{"role": "system", "content": "system prompt"}])

        self.assertEqual(result, '{"action":"final","answer":{}}')
        self.assertEqual(
            adapter.last_usage,
            {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        )

    def test_usage_maps_tokens_and_estimates_cost_when_rates_are_supplied(self) -> None:
        client = FakeOpenAIClient(ObjectResponse())
        adapter = OpenAIModelClient(
            client=client,
            input_cost_per_million=2.0,
            output_cost_per_million=8.0,
        )

        adapter.complete([{"role": "user", "content": "hello"}])

        self.assertEqual(adapter.last_usage["prompt_tokens"], 1000)
        self.assertEqual(adapter.last_usage["completion_tokens"], 200)
        self.assertEqual(adapter.last_usage["total_tokens"], 1200)
        self.assertAlmostEqual(adapter.last_usage["estimated_cost_usd"], 0.0036)

    def test_missing_output_text_raises_clear_error(self) -> None:
        adapter = OpenAIModelClient(client=FakeOpenAIClient({"output": []}))

        with self.assertRaisesRegex(RuntimeError, "did not contain output text"):
            adapter.complete([{"role": "user", "content": "hello"}])

    def test_incomplete_response_error_includes_reason(self) -> None:
        response = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "reasoning", "content": []}],
        }
        adapter = OpenAIModelClient(client=FakeOpenAIClient(response))

        with self.assertRaisesRegex(RuntimeError, "max_output_tokens"):
            adapter.complete([{"role": "user", "content": "hello"}])

    def test_worker_records_openai_adapter_usage_metadata(self) -> None:
        repo = FakeRepository()
        repo.add_run()
        adapter = OpenAIModelClient(
            client=FakeOpenAIClient(ObjectResponse()),
            estimated_call_cost_usd=0.0036,
            input_cost_per_million=2.0,
            output_cost_per_million=8.0,
        )
        worker = AgentWorker(repo, adapter, ToolExecutor(repo, ToolRegistry()))

        result = worker.process_run("run_1")

        self.assertEqual(result, "completed")
        completed = [
            event for event in repo.events if event["event_type"] == "model_call_completed"
        ]
        self.assertEqual(len(completed), 1)
        payload = completed[0]["payload"]
        self.assertEqual(payload["prompt_tokens"], 1000)
        self.assertEqual(payload["completion_tokens"], 200)
        self.assertEqual(payload["total_tokens"], 1200)
        self.assertAlmostEqual(payload["estimated_cost_usd"], 0.0036)


if __name__ == "__main__":
    unittest.main()
