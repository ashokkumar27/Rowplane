from __future__ import annotations

import unittest

from helpers import FakeRepository, SRC  # noqa: F401
from rowplane.runtime.commands import ToolCommand
from rowplane.runtime.errors import ToolPermissionDenied, ToolValidationError
from rowplane.tools.base import ToolDefinition
from rowplane.tools.executor import ToolExecutor
from rowplane.tools.registry import ToolRegistry


class ToolExecutorTests(unittest.TestCase):
    def make_executor(self, repo: FakeRepository, calls: list[dict]) -> ToolExecutor:
        registry = ToolRegistry()

        def handler(context, arguments):
            calls.append(dict(arguments))
            return {"ok": True, "x": arguments["x"]}

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
        return ToolExecutor(repo, registry)

    def test_executes_registered_permitted_tool_and_replays_idempotently(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        tool = repo.add_tool("tenant_1", "demo_tool")
        repo.grant_tenant("tenant_1", tool["id"])
        calls: list[dict] = []
        executor = self.make_executor(repo, calls)
        command = ToolCommand(action="tool", tool_name="demo_tool", arguments={"x": 7})

        outcome = executor.execute(run, command)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(repo.runs["run_1"]["status"], "queued")
        self.assertEqual(calls, [{"x": 7}])
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("tool_started", event_types)
        self.assertIn("tool_completed", event_types)

        repo.update_run_status("run_1", "queued", "thinking")
        outcome = executor.execute(repo.runs["run_1"], command)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls, [{"x": 7}])
        event_types = [event["event_type"] for event in repo.events]
        self.assertIn("tool_execution_replayed", event_types)

    def test_permission_denial_blocks_execution(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        repo.add_tool("tenant_1", "demo_tool")
        calls: list[dict] = []
        executor = self.make_executor(repo, calls)

        with self.assertRaises(ToolPermissionDenied):
            executor.execute(run, ToolCommand("tool", "demo_tool", {"x": 1}))

        self.assertEqual(calls, [])
        self.assertEqual(repo.events[-1]["event_type"], "tool_permission_denied")

    def test_schema_validation_rejects_bad_arguments_before_execution(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        tool = repo.add_tool("tenant_1", "demo_tool")
        repo.grant_tenant("tenant_1", tool["id"])
        calls: list[dict] = []
        executor = self.make_executor(repo, calls)

        with self.assertRaises(ToolValidationError):
            executor.execute(run, ToolCommand("tool", "demo_tool", {"x": "bad"}))

        self.assertEqual(calls, [])
        self.assertEqual(repo.events[-1]["event_type"], "tool_validation_failed")


    def test_output_schema_rejects_bad_tool_result(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        tool = repo.add_tool("tenant_1", "demo_tool")
        repo.grant_tenant("tenant_1", tool["id"])
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="demo_tool",
                handler=lambda context, arguments: {"ok": "not_boolean"},
                input_schema={"type": "object"},
                output_schema={
                    "type": "object",
                    "required": ["ok"],
                    "properties": {"ok": {"type": "boolean"}},
                    "additionalProperties": False,
                },
            )
        )
        executor = ToolExecutor(repo, registry)

        outcome = executor.execute(run, ToolCommand("tool", "demo_tool", {}))

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(repo.runs["run_1"]["status"], "queued")
        self.assertEqual(repo.events[-1]["event_type"], "run_status_changed")
        self.assertIn("tool_failed", [event["event_type"] for event in repo.events])

    def test_approval_policy_requires_approval_dynamically(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        tool = repo.add_tool(
            "tenant_1",
            "demo_tool",
            approval_policy={"rules": [{"field": "x", "operator": "gte", "value": 10}]},
        )
        repo.grant_tenant("tenant_1", tool["id"])
        calls: list[dict] = []
        executor = self.make_executor(repo, calls)

        outcome = executor.execute(run, ToolCommand("tool", "demo_tool", {"x": 12}))

        self.assertEqual(outcome.status, "waiting_approval")
        self.assertEqual(calls, [])
        approval = next(iter(repo.approvals.values()))
        self.assertEqual(approval["payload"]["tool_name"], "demo_tool")


    def test_malformed_numeric_approval_policy_does_not_crash_or_gate(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        tool = repo.add_tool(
            "tenant_1",
            "demo_tool",
            approval_policy={"rules": [{"field": "x", "operator": "gte", "value": "not-a-number"}]},
        )
        repo.grant_tenant("tenant_1", tool["id"])
        calls: list[dict] = []
        executor = self.make_executor(repo, calls)

        outcome = executor.execute(run, ToolCommand("tool", "demo_tool", {"x": 12}))

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls, [{"x": 12}])
        self.assertEqual(repo.approvals, {})

    def test_approval_required_then_resume_after_approval(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        tool = repo.add_tool("tenant_1", "demo_tool", requires_approval=True)
        repo.grant_tenant("tenant_1", tool["id"])
        calls: list[dict] = []
        executor = self.make_executor(repo, calls)
        command = ToolCommand("tool", "demo_tool", {"x": 3})

        outcome = executor.execute(run, command)

        self.assertEqual(outcome.status, "waiting_approval")
        self.assertEqual(calls, [])
        self.assertEqual(len(repo.approvals), 1)
        self.assertEqual(repo.runs["run_1"]["status"], "waiting_approval")

        approval_id = next(iter(repo.approvals))
        repo.resolve_approval_request(approval_id, "approved", "human")
        repo.update_run_status("run_1", "waiting_approval", "queued")
        repo.update_run_status("run_1", "queued", "thinking")

        outcome = executor.execute(repo.runs["run_1"], command)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls, [{"x": 3}])
        self.assertEqual(len(repo.approvals), 1)

    def test_pending_approval_replay_does_not_duplicate_request(self) -> None:
        repo = FakeRepository()
        run = repo.add_run(status="thinking")
        tool = repo.add_tool("tenant_1", "demo_tool", requires_approval=True)
        repo.grant_tenant("tenant_1", tool["id"])
        executor = self.make_executor(repo, [])
        command = ToolCommand("tool", "demo_tool", {"x": 5})

        executor.execute(run, command)
        repo.update_run_status("run_1", "waiting_approval", "queued")
        repo.update_run_status("run_1", "queued", "thinking")
        executor.execute(repo.runs["run_1"], command)

        self.assertEqual(len(repo.approvals), 1)
        self.assertEqual(repo.runs["run_1"]["status"], "waiting_approval")


if __name__ == "__main__":
    unittest.main()
