from __future__ import annotations

import unittest

from helpers import FakeRepository, SRC  # noqa: F401
from rowplane.approvals.service import ApprovalService
from rowplane.runtime.errors import ApprovalAlreadyResolved


class ApprovalServiceTests(unittest.TestCase):
    def test_approved_request_requeues_run(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="waiting_approval")
        approval = repo.create_approval_request("tenant_1", "run_1", "Need approval", {})

        resolved = ApprovalService(repo).resolve(
            approval["id"],
            approved=True,
            resolved_by="human_1",
        )

        self.assertEqual(resolved["status"], "approved")
        self.assertEqual(repo.runs["run_1"]["status"], "queued")
        self.assertEqual(repo.queued, [("tenant_1", "run_1")])
        self.assertEqual(repo.events[-2]["event_type"], "approval_resolved")

    def test_rejected_request_blocks_run(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="waiting_approval")
        approval = repo.create_approval_request("tenant_1", "run_1", "Need approval", {})

        ApprovalService(repo).resolve(approval["id"], approved=False, resolved_by="human_1")

        self.assertEqual(repo.runs["run_1"]["status"], "blocked")
        self.assertIn("rejected", repo.runs["run_1"]["error"])


    def test_rejected_task_request_blocks_child_and_requeues_parent(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="thinking")
        parent_agent = repo.add_agent("planner", agent_id="agent_planner")
        child_agent = repo.add_agent("operator", agent_id="agent_operator")
        repo.add_task(parent_agent["id"], task_id="task_parent", status="waiting_child")
        repo.add_task(
            child_agent["id"],
            task_id="task_child",
            parent_task_id="task_parent",
            status="waiting_approval",
        )
        approval = repo.create_approval_request(
            "tenant_1",
            "run_1",
            "Need approval",
            {},
            task_id="task_child",
        )

        ApprovalService(repo).resolve(approval["id"], approved=False, resolved_by="human_1")

        self.assertEqual(repo.tasks["task_child"]["status"], "blocked")
        self.assertEqual(repo.tasks["task_parent"]["status"], "queued")
        self.assertEqual(repo.queued_tasks, [("tenant_1", "run_1", "task_parent")])
        self.assertEqual(repo.messages[-1]["message_type"], "task_result")

    def test_resolved_request_cannot_be_resolved_again(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="waiting_approval")
        approval = repo.create_approval_request("tenant_1", "run_1", "Need approval", {})
        service = ApprovalService(repo)

        service.resolve(approval["id"], approved=True, resolved_by="human_1")

        with self.assertRaises(ApprovalAlreadyResolved):
            service.resolve(approval["id"], approved=True, resolved_by="human_2")


if __name__ == "__main__":
    unittest.main()
