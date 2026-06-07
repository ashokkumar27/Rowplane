from __future__ import annotations

import unittest
from typing import Any, Mapping

from helpers import FakeRepository, SRC, StaticModel  # noqa: F401
from rowplane.tools.registry import ToolRegistry
from rowplane.workers.lease_worker import AgentLeaseWorker


def lease_worker(repo: FakeRepository, response: Mapping[str, Any]) -> AgentLeaseWorker:
    return AgentLeaseWorker(
        repo,
        StaticModel(response),
        ToolRegistry(),
        worker_id="worker_1",
        tenant_id="tenant_1",
    )


class AgentLeaseWorkerTests(unittest.TestCase):
    def test_run_once_claims_run_processes_and_completes_lease(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        worker = lease_worker(repo, {"action": "final", "answer": {"ok": True}})

        outcome = worker.run_once()

        self.assertEqual(outcome, "completed")
        self.assertEqual(repo.runs["run_1"]["status"], "completed")
        self.assertEqual(repo.runs["run_1"]["answer"], {"ok": True})
        lease = next(iter(repo.work_leases.values()))
        self.assertEqual(lease["status"], "completed")
        self.assertEqual(lease["metadata"], {"outcome": "completed", "work_type": "run"})
        self.assertEqual(repo.tenant_context, "tenant_1")
        self.assertIn("work_claimed", [event["event_type"] for event in repo.events])
        self.assertIn("work_lease_completed", [event["event_type"] for event in repo.events])

    def test_run_once_claims_task_before_run_and_processes_task(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        agent = repo.add_agent("planner", agent_id="agent_planner")
        repo.add_task(agent["id"], task_id="task_root", status="queued")
        worker = lease_worker(repo, {"action": "final", "answer": {"status": "done"}})

        outcome = worker.run_once()

        self.assertEqual(outcome, "completed")
        self.assertEqual(repo.tasks["task_root"]["status"], "completed")
        self.assertEqual(repo.runs["run_1"]["status"], "completed")
        lease = next(iter(repo.work_leases.values()))
        self.assertEqual(lease["work_type"], "task")
        self.assertEqual(lease["status"], "completed")

    def test_releases_lease_for_ignored_work(self) -> None:
        repo = FakeRepository()
        repo.add_run(status="queued")
        claim = repo.claim_agent_work("tenant_1", "worker_1", kinds=["run"])[0]
        repo.runs["run_1"]["status"] = "thinking"
        worker = lease_worker(repo, {"action": "final", "answer": {}})

        outcome = worker.process_claim(claim)

        self.assertEqual(outcome, "ignored")
        lease = repo.work_leases[str(claim["work_lease_id"])]
        self.assertEqual(lease["status"], "released")
        self.assertEqual(lease["metadata"], {"outcome": "ignored", "work_type": "run"})

    def test_run_once_returns_empty_when_no_claim_available(self) -> None:
        repo = FakeRepository()
        worker = lease_worker(repo, {"action": "final", "answer": {}})

        self.assertEqual(worker.run_once(), "empty")
        self.assertEqual(repo.work_leases, {})


if __name__ == "__main__":
    unittest.main()
