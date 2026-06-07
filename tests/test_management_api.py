from __future__ import annotations

import unittest
from typing import Any, Mapping

from helpers import FakeRepository, SRC  # noqa: F401

from fastapi.testclient import TestClient
from rowplane.management.api import create_app


class FakeManagementRepository(FakeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.audit_events: list[dict[str, Any]] = []

    def management_overview(self, tenant_id: str) -> Mapping[str, Any]:
        run_counts: dict[str, int] = {}
        task_counts: dict[str, int] = {}
        for run in self.runs.values():
            if run["tenant_id"] == tenant_id:
                run_counts[run["status"]] = run_counts.get(run["status"], 0) + 1
        for task in self.tasks.values():
            if task["tenant_id"] == tenant_id:
                task_counts[task["status"]] = task_counts.get(task["status"], 0) + 1
        pending = sum(1 for item in self.approvals.values() if item["tenant_id"] == tenant_id and item["status"] == "pending")
        return {
            "run_status_counts": run_counts,
            "task_status_counts": task_counts,
            "pending_approvals": pending,
            "blocked_runs": run_counts.get("blocked", 0),
            "queue_backlog": {"runs": run_counts.get("queued", 0), "tasks": task_counts.get("queued", 0), "total": run_counts.get("queued", 0) + task_counts.get("queued", 0)},
            "tool_failure_rate": None,
            "eval_pass_rate": None,
            "recent_events": self.load_events("run_1"),
        }

    def list_management_approvals(self, tenant_id: str, *, status: str | None = "pending", run_id: str | None = None, task_id: str | None = None, tool_name: str | None = None, limit: int = 50, offset: int = 0):
        rows = []
        for approval in self.approvals.values():
            if approval["tenant_id"] != tenant_id:
                continue
            if status and approval["status"] != status:
                continue
            if run_id and approval["run_id"] != run_id:
                continue
            if task_id and approval.get("task_id") != task_id:
                continue
            rows.append({**approval, "approval_request_id": approval["id"], "tool_name": tool_name})
        return rows[offset: offset + limit]

    def get_management_approval(self, approval_id: str):
        approval = self.approvals.get(approval_id)
        if approval is None:
            return None
        return {
            "approval": {**approval, "approval_request_id": approval_id},
            "run": self.runs.get(approval["run_id"]),
            "task": self.tasks.get(approval.get("task_id")),
            "tool_execution": self.executions.get(approval.get("tool_execution_id")),
            "recent_events": self.load_events(approval["run_id"]),
        }

    def list_management_runs(self, tenant_id: str, *, status: str | None = None, agent_id: str | None = None, eval_case_id: str | None = None, limit: int = 50, offset: int = 0):
        rows = []
        for run in self.runs.values():
            if run["tenant_id"] != tenant_id:
                continue
            if status and run["status"] != status:
                continue
            rows.append({"run_id": run["id"], "status": run["status"], "model": run["model"], "pending_approval_count": 0})
        return rows[offset: offset + limit]

    def get_management_run(self, run_id: str):
        run = self.runs.get(run_id)
        if run is None:
            return None
        return {
            "run": run,
            "tasks": [task for task in self.tasks.values() if task["run_id"] == run_id],
            "messages": [message for message in self.messages if message["run_id"] == run_id],
            "approvals": [approval for approval in self.approvals.values() if approval["run_id"] == run_id],
            "tool_executions": [execution for execution in self.executions.values() if execution["run_id"] == run_id],
        }

    def get_management_run_timeline(self, run_id: str, *, limit: int = 300):
        return [
            {"kind": "event", "type": event["event_type"], "created_at": "2026-01-01T00:00:00Z", "data": event}
            for event in self.load_events(run_id, limit=limit)
        ]

    def retry_management_run(self, tenant_id: str, run_id: str):
        source = self.runs.get(run_id)
        if source is None:
            raise LookupError(f"run not found: {run_id}")
        if source["status"] not in {"failed", "blocked"}:
            raise ValueError("only failed or blocked runs can be retried")
        retry_id = self.next_id("run")
        retry = self.add_run(run_id=retry_id, tenant_id=tenant_id, status="queued")
        retry["task"] = dict(source["task"])
        self.queue_run(tenant_id, retry_id)
        self.append_event(tenant_id, run_id, "retry_created", {"new_run_id": retry_id}, actor="management_api")
        return retry

    def list_management_tools(self, tenant_id: str):
        return [tool for (tool_tenant, _), tool in self.tools.items() if tool_tenant == tenant_id]

    def update_management_tool(self, tool_id: str, *, enabled=None, requires_approval=None, description=None, actor="management_api"):
        for tool in self.tools.values():
            if tool["id"] == tool_id:
                if enabled is not None:
                    tool["enabled"] = enabled
                if requires_approval is not None:
                    tool["requires_approval"] = requires_approval
                if description is not None:
                    tool["description"] = description
                self.audit_events.append({"event_type": "tool_updated", "actor": actor, "payload": {"tool_id": tool_id}})
                return tool
        raise LookupError(f"tool not found: {tool_id}")

    def list_management_agents(self, tenant_id: str):
        return [agent for agent in self.agents.values() if agent["tenant_id"] == tenant_id]

    def get_management_agent(self, agent_id: str):
        agent = self.agents.get(agent_id)
        if agent is None:
            return None
        return {"agent": agent, "tool_permissions": [], "task_status_counts": {}}

    def list_management_eval_summaries(self, tenant_id: str):
        return list(self.eval_results.values())

    def list_management_eval_results(self, tenant_id: str, eval_case_id: str, *, limit: int = 50, offset: int = 0):
        return [result for result in self.eval_results.values() if result["tenant_id"] == tenant_id and result["eval_case_id"] == eval_case_id][offset: offset + limit]

    def list_management_audit_events(self, tenant_id: str, *, run_id=None, event_type=None, actor=None, limit: int = 100, offset: int = 0):
        rows = [{"source": "agent_event", **event} for event in self.events if event["tenant_id"] == tenant_id]
        rows.extend({"source": "audit_event", **event, "tenant_id": tenant_id, "run_id": None} for event in self.audit_events)
        if event_type:
            rows = [row for row in rows if row["event_type"] == event_type]
        if actor:
            rows = [row for row in rows if row["actor"] == actor]
        return rows[offset: offset + limit]

    def list_management_memory(self, tenant_id: str, *, memory_type=None, source_run_id=None, limit: int = 50, offset: int = 0):
        rows = [memory for memory in self.memories.values() if memory["tenant_id"] == tenant_id]
        return rows[offset: offset + limit]

    def get_management_run_trajectory(self, tenant_id: str, run_id: str, *, limit: int = 500):
        return [
            {
                "source": "event",
                "sequence_id": event.get("event_id", index + 1),
                "step_type": event["event_type"],
                "actor": event["actor"],
                "payload": event["payload"],
            }
            for index, event in enumerate(self.events)
            if event["tenant_id"] == tenant_id and event["run_id"] == run_id
        ][:limit]

    def search_management_harness(self, tenant_id: str, query: str, *, limit: int = 50):
        rows = []
        for event in self.events:
            if event["tenant_id"] == tenant_id and query in event["event_type"]:
                rows.append({"source": "event", "id": str(event.get("event_id", len(rows) + 1)), "run_id": event["run_id"], "snippet": event["event_type"], "payload": event["payload"], "rank": 1.0})
        return rows[:limit]


class ManagementApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = FakeManagementRepository()
        self.repo.add_run(status="waiting_approval")
        self.repo.add_agent("planner", agent_id="agent_1")
        self.repo.add_task("agent_1", task_id="task_1", status="waiting_approval")
        tool = self.repo.add_tool("tenant_1", "issue_refund", requires_approval=True)
        execution = self.repo.create_tool_execution("tenant_1", "run_1", tool["id"], "idem", {"amount": 10}, "hash", task_id="task_1")
        self.approval = self.repo.create_approval_request("tenant_1", "run_1", "Need approval", {"tool_name": "issue_refund"}, tool_execution_id=execution["id"], task_id="task_1")
        self.repo.append_event("tenant_1", "run_1", "approval_requested", {"approval_request_id": self.approval["id"]})
        app = create_app(repository_factory=lambda: self.repo)
        self.client = TestClient(app)
        self.headers = {"X-Tenant-ID": "tenant_1", "X-Actor": "admin_1"}

    def test_overview_and_approval_list_are_tenant_scoped(self) -> None:
        overview = self.client.get("/api/metrics/overview", headers=self.headers)
        approvals = self.client.get("/api/approvals", headers=self.headers)

        self.assertEqual(overview.status_code, 200)
        self.assertEqual(overview.json()["pending_approvals"], 1)
        self.assertEqual(approvals.status_code, 200)
        self.assertEqual(approvals.json()[0]["approval_request_id"], self.approval["id"])
        self.assertEqual(self.repo.tenant_context, "tenant_1")

    def test_approval_can_be_approved_through_existing_service(self) -> None:
        response = self.client.post(f"/api/approvals/{self.approval['id']}/approve", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "approved")
        self.assertEqual(self.repo.tasks["task_1"]["status"], "queued")
        self.assertEqual(self.repo.queued_tasks, [("tenant_1", "run_1", "task_1")])

    def test_run_detail_timeline_trajectory_search_and_retry(self) -> None:
        detail = self.client.get("/api/runs/run_1", headers=self.headers)
        timeline = self.client.get("/api/runs/run_1/timeline", headers=self.headers)
        trajectory = self.client.get("/api/runs/run_1/trajectory", headers=self.headers)
        search = self.client.get("/api/search?q=approval", headers=self.headers)
        self.repo.runs["run_1"]["status"] = "failed"
        retry = self.client.post("/api/runs/run_1/retry", headers=self.headers)

        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["tasks"]), 1)
        self.assertEqual(timeline.status_code, 200)
        self.assertEqual(timeline.json()[0]["kind"], "event")
        self.assertEqual(trajectory.status_code, 200)
        self.assertEqual(trajectory.json()[0]["source"], "event")
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()[0]["source"], "event")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.json()["status"], "queued")
        self.assertEqual(self.repo.runs["run_1"]["status"], "failed")

    def test_tool_update_is_audited(self) -> None:
        tool_id = next(iter(self.repo.tools.values()))["id"]

        response = self.client.patch(
            f"/api/tools/{tool_id}",
            headers=self.headers,
            json={"enabled": False, "requires_approval": True},
        )
        audit = self.client.get("/api/audit/events?event_type=tool_updated", headers=self.headers)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])
        self.assertEqual(audit.status_code, 200)
        self.assertEqual(audit.json()[0]["actor"], "admin_1")

    def test_empty_tool_update_is_rejected(self) -> None:
        tool_id = next(iter(self.repo.tools.values()))["id"]

        response = self.client.patch(
            f"/api/tools/{tool_id}",
            headers=self.headers,
            json={},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.repo.audit_events, [])

    def test_console_assets_are_served(self) -> None:
        console = self.client.get("/console")
        asset = self.client.get("/console/assets/app.js")

        self.assertEqual(console.status_code, 200)
        self.assertIn("Rowplane Console", console.text)
        self.assertEqual(asset.status_code, 200)
        self.assertIn("loadApprovals", asset.text)


if __name__ == "__main__":
    unittest.main()
