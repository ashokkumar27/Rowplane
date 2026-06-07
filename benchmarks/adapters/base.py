"""Framework adapter contracts and shared live-agent implementation."""

from __future__ import annotations

import importlib.util
import time
from abc import ABC, abstractmethod
from typing import Any

from benchmarks.openai_client import OpenAIJsonClient, json_dumps
from benchmarks.scoring import score_run
from benchmarks.toolbox import BenchmarkToolbox
from benchmarks.types import BenchmarkRunRecord, BenchmarkScenario


class FrameworkAdapter(ABC):
    name: str

    @abstractmethod
    def run(self, scenario: BenchmarkScenario, *, repeat: int, model: str) -> BenchmarkRunRecord:
        raise NotImplementedError


class PortableToolLoopAdapter(FrameworkAdapter):
    """A strict JSON tool loop used by competitor wrappers.

    The adapter still checks that the competitor dependency is importable. It
    avoids hidden product-specific glue by exposing the same tool catalog to
    every framework wrapper and scoring the evidence that results.
    """

    package_name: str | None = None

    def run(self, scenario: BenchmarkScenario, *, repeat: int, model: str) -> BenchmarkRunRecord:
        record = BenchmarkRunRecord(
            framework=self.name,
            scenario=scenario.name,
            repeat=repeat,
            model=model,
        )
        started = time.perf_counter()
        if self.package_name and importlib.util.find_spec(self.package_name) is None:
            record.errors.append(
                f"missing dependency '{self.package_name}'; install benchmarks/requirements.txt"
            )
            record.latency_ms = _elapsed_ms(started)
            record.score = score_run(record, scenario)
            return record

        toolbox = BenchmarkToolbox(record)
        try:
            client = OpenAIJsonClient(model=model)
            answer = self._run_tool_loop(client, scenario, toolbox)
            record.answer = answer
        except Exception as exc:
            record.errors.append(str(exc))
        finally:
            record.latency_ms = _elapsed_ms(started)
            record.score = score_run(record, scenario)
        return record

    def _run_tool_loop(
        self,
        client: OpenAIJsonClient,
        scenario: BenchmarkScenario,
        toolbox: BenchmarkToolbox,
    ) -> dict[str, Any] | None:
        system = (
            f"You are running benchmark adapter {self.name}. "
            "Return exactly one JSON object. Use this command contract: "
            '{"action":"tool","tool_name":"...","arguments":{}} or '
            '{"action":"final","answer":{}}. '
            "Never invent tool results. Call tools when the scenario requires them."
        )
        transcript: list[dict[str, Any]] = [{"role": "user", "content": scenario.prompt}]
        result: dict[str, Any] | None = None
        for _ in range(scenario.max_turns):
            user = json_dumps(
                {
                    "scenario": scenario.name,
                    "prompt": scenario.prompt,
                    "tools": [tool.__dict__ for tool in scenario.tools],
                    "expected_json_shape": scenario.expected,
                    "transcript": transcript,
                }
            )
            response = client.complete_json(system, user)
            command = response.value
            result = command
            self._record_usage(toolbox.record, response)
            if command.get("action") == "final":
                answer = command.get("answer")
                return answer if isinstance(answer, dict) else {"raw": answer}
            if command.get("action") != "tool":
                return command
            tool_name = str(command.get("tool_name", ""))
            arguments = command.get("arguments") if isinstance(command.get("arguments"), dict) else {}
            tool_result = self._call_tool(toolbox, tool_name, arguments)
            transcript.append({"role": "assistant", "content": command})
            transcript.append({"role": "tool", "name": tool_name, "content": tool_result})
        return result

    def _call_tool(
        self,
        toolbox: BenchmarkToolbox,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if tool_name == "search_policy_documents":
            return toolbox.search_policy_documents(
                str(arguments.get("query", "")),
                int(arguments.get("top_k", 2)),
            )
        if tool_name == "request_approval":
            payload = arguments.get("payload") if isinstance(arguments.get("payload"), dict) else {}
            return toolbox.request_approval(str(arguments.get("reason", "")), payload)
        if tool_name == "issue_refund":
            return toolbox.issue_refund(
                str(arguments.get("customer_id", "")),
                int(arguments.get("amount_cents", 0)),
                str(arguments.get("reason", "")),
            )
        if tool_name == "export_customer_data":
            return toolbox.export_customer_data(str(arguments.get("scope", "")))
        if tool_name == "search_tenant_memory":
            return toolbox.search_tenant_memory(str(arguments.get("query", "")))
        result = {"status": "unknown_tool", "tool_name": tool_name}
        toolbox._record_tool(tool_name, arguments, result)
        return result

    def _record_usage(self, record: BenchmarkRunRecord, response: Any) -> None:
        usage = response.usage
        record.input_tokens = (record.input_tokens or 0) + (usage.input_tokens or 0)
        record.output_tokens = (record.output_tokens or 0) + (usage.output_tokens or 0)
        if usage.estimated_cost_usd is not None:
            record.estimated_cost_usd = (record.estimated_cost_usd or 0.0) + usage.estimated_cost_usd


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
