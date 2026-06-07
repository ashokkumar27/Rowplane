from __future__ import annotations

import unittest
from typing import Any, Mapping, Sequence

from helpers import FakeRepository, SRC  # noqa: F401
from rowplane.runtime.commands import ToolCommand
from rowplane.tools.base import ToolDefinition
from rowplane.tools.registry import ToolRegistry
from rowplane.tools.task_executor import TaskToolExecutor
from rowplane.workers.task_worker import AgentTaskWorker


class SequenceModel:
    def __init__(self, responses: Sequence[Mapping[str, Any]]) -> None:
        self.responses = list(responses)
        self.messages: list[Any] = []

    def complete(self, messages: Any) -> Mapping[str, Any]:
        self.messages.append(messages)
        if not self.responses:
            return {"action": "fail", "reason": "no scripted response"}
        return self.responses.pop(0)


def empty_task_worker(repo: FakeRepository, responses: Sequence[Mapping[str, Any]]) -> AgentTaskWorker:
    return AgentTaskWorker(repo, SequenceModel(responses), TaskToolExecutor(repo, ToolRegistry()))


class MultiAgentWorkerTests(unittest.TestCase):
    def test_model_call_budget_denial_blocks_task_before_model_call(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        repo.add_task(planner["id"], task_id="task_root")
        repo.add_runtime_budget(scope_type="task", scope_id="task_root", max_model_calls=0)
        model = SequenceModel([{"action": "final", "answer": {"ok": True}}])
        worker = AgentTaskWorker(repo, model, TaskToolExecutor(repo, ToolRegistry()))

        result = worker.process_task("task_root")

        self.assertEqual(result, "blocked")
        self.assertEqual(model.messages, [])
        self.assertEqual(repo.tasks["task_root"]["status"], "blocked")
        self.assertEqual(repo.runs["run_1"]["status"], "blocked")
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("runtime_budget_exceeded", event_types)
        self.assertIn("model_call_denied_by_budget", event_types)
        self.assertEqual(event_types.count("task_blocked"), 1)

    def test_model_call_reservation_written_before_allowed_task_model_call(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        repo.add_task(planner["id"], task_id="task_root")
        repo.add_runtime_budget(scope_type="task", scope_id="task_root", max_model_calls=1)
        model = SequenceModel([{"action": "final", "answer": {"ok": True}}])
        worker = AgentTaskWorker(repo, model, TaskToolExecutor(repo, ToolRegistry()))

        result = worker.process_task("task_root")

        self.assertEqual(result, "completed")
        self.assertEqual(len(model.messages), 1)
        self.assertIn("model_call_reserved", [event["event_type"] for event in repo.events])

    def test_delegate_rejected_when_child_task_budget_exceeded(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        repo.add_agent("policy_researcher", agent_id="agent_researcher")
        repo.add_task(planner["id"], task_id="task_root")
        repo.add_runtime_budget(scope_type="task", scope_id="task_root", max_child_tasks=0)
        worker = empty_task_worker(
            repo,
            [
                {
                    "action": "delegate",
                    "to_agent": "policy_researcher",
                    "task": {"question": "Find policy evidence."},
                    "reason": "Need specialist evidence.",
                }
            ],
        )

        result = worker.process_task("task_root")

        self.assertEqual(result, "blocked")
        self.assertEqual(repo.tasks["task_root"]["status"], "blocked")
        self.assertEqual(repo.runs["run_1"]["status"], "blocked")
        self.assertEqual([task for task in repo.tasks.values() if task["parent_task_id"] == "task_root"], [])
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("runtime_budget_exceeded", event_types)
        self.assertIn("delegation_rejected_by_budget", event_types)

    def test_delegate_creates_child_task_message_and_wakes_child(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        researcher = repo.add_agent("policy_researcher", agent_id="agent_researcher")
        repo.add_task(planner["id"], task_id="task_root")
        worker = empty_task_worker(
            repo,
            [
                {
                    "action": "delegate",
                    "to_agent": "policy_researcher",
                    "task": {"question": "Find policy evidence."},
                    "reason": "Need specialist evidence.",
                }
            ],
        )

        result = worker.process_task("task_root")

        child_tasks = [task for task in repo.tasks.values() if task["parent_task_id"] == "task_root"]
        self.assertEqual(result, "waiting_child")
        self.assertEqual(repo.runs["run_1"]["status"], "thinking")
        self.assertEqual(repo.tasks["task_root"]["status"], "waiting_child")
        self.assertEqual(len(child_tasks), 1)
        self.assertEqual(child_tasks[0]["agent_id"], researcher["id"])
        self.assertEqual(repo.messages[-1]["message_type"], "delegation")
        self.assertEqual(repo.queued_tasks, [("tenant_1", "run_1", child_tasks[0]["id"])])
        self.assertIn("delegation_created", [event["event_type"] for event in repo.events])

    def test_child_final_reports_result_and_requeues_parent(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="thinking")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        researcher = repo.add_agent("policy_researcher", agent_id="agent_researcher")
        repo.add_task(planner["id"], task_id="task_parent", status="waiting_child")
        repo.add_task(
            researcher["id"],
            task_id="task_child",
            parent_task_id="task_parent",
            status="queued",
        )
        worker = empty_task_worker(
            repo,
            [
                {
                    "action": "final",
                    "answer": {"citations": ["policy:dpa", "policy:soc2"]},
                }
            ],
        )

        result = worker.process_task("task_child")

        self.assertEqual(result, "completed")
        self.assertEqual(repo.tasks["task_child"]["status"], "completed")
        self.assertEqual(repo.tasks["task_parent"]["status"], "queued")
        self.assertEqual(repo.runs["run_1"]["status"], "thinking")
        self.assertEqual(repo.messages[-1]["message_type"], "task_result")
        self.assertEqual(repo.queued_tasks, [("tenant_1", "run_1", "task_parent")])


    def test_parent_waits_until_all_required_child_dependencies_are_satisfied(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="thinking")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        researcher = repo.add_agent("policy_researcher", agent_id="agent_researcher")
        repo.add_task(planner["id"], task_id="task_parent", status="waiting_child")
        repo.add_task(researcher["id"], task_id="task_child_1", parent_task_id="task_parent", status="queued")
        repo.add_task(researcher["id"], task_id="task_child_2", parent_task_id="task_parent", status="queued")
        repo.create_task_dependency("tenant_1", "run_1", "task_parent", "task_child_1")
        repo.create_task_dependency("tenant_1", "run_1", "task_parent", "task_child_2")

        first = empty_task_worker(repo, [{"action": "final", "answer": {"child": 1}}]).process_task("task_child_1")

        self.assertEqual(first, "completed")
        self.assertEqual(repo.tasks["task_parent"]["status"], "waiting_child")

        second = empty_task_worker(repo, [{"action": "final", "answer": {"child": 2}}]).process_task("task_child_2")

        self.assertEqual(second, "completed")
        self.assertEqual(repo.tasks["task_parent"]["status"], "queued")
        self.assertEqual(repo.queued_tasks, [("tenant_1", "run_1", "task_parent")])
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("task_dependency_satisfied", event_types)
        self.assertIn("task_dependency_parent_released", event_types)

    def test_required_child_dependency_failure_blocks_parent(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="thinking")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        researcher = repo.add_agent("policy_researcher", agent_id="agent_researcher")
        repo.add_task(planner["id"], task_id="task_parent", status="waiting_child")
        repo.add_task(researcher["id"], task_id="task_child", parent_task_id="task_parent", status="queued")
        repo.create_task_dependency("tenant_1", "run_1", "task_parent", "task_child")

        result = empty_task_worker(repo, [{"action": "fail", "reason": "missing evidence"}]).process_task("task_child")

        self.assertEqual(result, "failed")
        self.assertEqual(repo.tasks["task_parent"]["status"], "blocked")
        self.assertEqual(repo.tasks["task_parent"]["error"], "required child task dependency failed")
        self.assertIn("task_dependency_parent_blocked", [event["event_type"] for event in repo.events])

    def test_child_final_answer_contract_rejects_invalid_output_and_requeues_task(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="thinking")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        researcher = repo.add_agent("policy_researcher", agent_id="agent_researcher")
        repo.add_task(planner["id"], task_id="task_parent", status="waiting_child")
        repo.add_task(
            researcher["id"],
            task_id="task_child",
            parent_task_id="task_parent",
            status="queued",
            input={
                "question": "Find evidence.",
                "answer_contract": {
                    "schema": {
                        "type": "object",
                        "required": ["citations"],
                        "properties": {"citations": {"type": "array", "items": {"type": "string"}}},
                    }
                },
            },
        )
        worker = empty_task_worker(repo, [{"action": "final", "answer": {"summary": "missing citations"}}])

        result = worker.process_task("task_child")

        self.assertEqual(result, "queued")
        self.assertEqual(repo.tasks["task_child"]["status"], "queued")
        self.assertEqual(repo.tasks["task_parent"]["status"], "waiting_child")
        self.assertEqual(repo.queued_tasks, [("tenant_1", "run_1", "task_child")])
        self.assertIn("final_answer_rejected", [event["event_type"] for event in repo.events])
        self.assertEqual(repo.messages, [])

    def test_root_final_completes_run(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        repo.add_task(planner["id"], task_id="task_root")
        worker = empty_task_worker(
            repo,
            [{"action": "final", "answer": {"status": "done"}}],
        )

        result = worker.process_task("task_root")

        self.assertEqual(result, "completed")
        self.assertEqual(repo.tasks["task_root"]["status"], "completed")
        self.assertEqual(repo.runs["run_1"]["status"], "completed")
        self.assertEqual(repo.runs["run_1"]["answer"], {"status": "done"})

    def test_run_once_sets_tenant_and_deletes_task_message(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        planner = repo.add_agent("planner", agent_id="agent_planner")
        repo.add_task(planner["id"], task_id="task_root")
        repo.add_queue_message("tenant_1", "run_1", msg_id=77, task_id="task_root")
        worker = empty_task_worker(
            repo,
            [{"action": "final", "answer": {"status": "done"}}],
        )

        result = worker.run_once()

        self.assertEqual(result, "completed")
        self.assertEqual(repo.tenant_context, "tenant_1")
        self.assertEqual(repo.deleted_messages, [77])


class TaskToolExecutorTests(unittest.TestCase):
    def test_task_tool_uses_agent_permission_and_task_context(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        agent = repo.add_agent("operator", agent_id="agent_operator")
        task = repo.add_task(agent["id"], task_id="task_tool", status="thinking")
        tool = repo.add_tool("tenant_1", "demo_tool")
        repo.grant_agent("tenant_1", tool["id"], agent["id"])
        calls: list[dict[str, Any]] = []
        registry = ToolRegistry()

        def handler(context: Any, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
            calls.append(
                {
                    "arguments": dict(arguments),
                    "task_id": context.task_id,
                    "agent_id": context.agent_id,
                }
            )
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

        outcome = TaskToolExecutor(repo, registry).execute(
            run,
            task,
            agent,
            ToolCommand("tool", "demo_tool", {"x": 7}),
        )

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(repo.tasks["task_tool"]["status"], "queued")
        self.assertEqual(repo.queued_tasks, [("tenant_1", "run_1", "task_tool")])
        self.assertEqual(
            calls,
            [{"arguments": {"x": 7}, "task_id": "task_tool", "agent_id": "agent_operator"}],
        )


if __name__ == "__main__":
    unittest.main()
