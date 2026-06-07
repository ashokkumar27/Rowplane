"""Lease-driven worker over the SQL-native scheduler kernel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pg_agent.tools.executor import ToolExecutor
from pg_agent.tools.registry import ToolRegistry
from pg_agent.tools.task_executor import TaskToolExecutor
from pg_agent.workers.task_worker import AgentTaskWorker
from pg_agent.workers.worker import AgentWorker, ModelClient


class LeaseWorkerRepository(Protocol):
    def set_tenant(self, tenant_id: str) -> None: ...

    def claim_agent_work(
        self,
        tenant_id: str,
        worker_id: str,
        *,
        capabilities: Sequence[str] | None = None,
        max_items: int = 1,
        lease_seconds: int = 60,
        kinds: Sequence[str] | None = None,
        actor: str = "scheduler",
    ) -> list[Mapping[str, Any]]: ...

    def complete_agent_work(
        self,
        work_lease_id: str,
        worker_id: str,
        *,
        status: str = "completed",
        metadata: Mapping[str, Any] | None = None,
        actor: str = "scheduler",
    ) -> Mapping[str, Any]: ...

    def heartbeat_agent_work(
        self,
        work_lease_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        actor: str = "scheduler",
    ) -> Mapping[str, Any]: ...


class AgentLeaseWorker:
    """Claim queued run/task work through Postgres leases and process it locally."""

    def __init__(
        self,
        repository: LeaseWorkerRepository,
        model_client: ModelClient,
        registry: ToolRegistry,
        *,
        worker_id: str,
        tenant_id: str,
        capabilities: Sequence[str] | None = None,
        lease_seconds: int = 60,
        kinds: Sequence[str] | None = None,
    ) -> None:
        self.repository = repository
        self.model_client = model_client
        self.registry = registry
        self.worker_id = worker_id
        self.tenant_id = tenant_id
        self.capabilities = list(capabilities or [])
        self.lease_seconds = lease_seconds
        self.kinds = list(kinds or ["task", "run"])

    def run_once(self) -> str:
        claims = self.repository.claim_agent_work(
            self.tenant_id,
            self.worker_id,
            capabilities=self.capabilities,
            max_items=1,
            lease_seconds=self.lease_seconds,
            kinds=self.kinds,
            actor="lease_worker",
        )
        if not claims:
            return "empty"
        return self.process_claim(claims[0])

    def process_claim(self, claim: Mapping[str, Any]) -> str:
        tenant_id = str(claim["tenant_id"])
        if hasattr(self.repository, "set_tenant"):
            self.repository.set_tenant(tenant_id)

        work_lease_id = str(claim["work_lease_id"])
        work_type = str(claim["work_type"])
        try:
            if work_type == "run":
                outcome = AgentWorker(
                    self.repository,
                    self.model_client,
                    ToolExecutor(self.repository, self.registry),
                ).process_run(str(claim["run_id"]))
            elif work_type == "task":
                task_id = claim.get("task_id")
                if task_id is None:
                    outcome = "missing_task"
                else:
                    outcome = AgentTaskWorker(
                        self.repository,
                        self.model_client,
                        TaskToolExecutor(self.repository, self.registry),
                    ).process_task(str(task_id))
            else:
                outcome = f"unsupported_work_type:{work_type}"
        except Exception as exc:
            self.repository.complete_agent_work(
                work_lease_id,
                self.worker_id,
                status="failed",
                metadata={"error": str(exc), "work_type": work_type},
                actor="lease_worker",
            )
            raise

        lease_status = "completed"
        if outcome in {"ignored", "missing", "missing_run", "missing_task"} or outcome.startswith("unsupported_work_type"):
            lease_status = "released"
        self.repository.complete_agent_work(
            work_lease_id,
            self.worker_id,
            status=lease_status,
            metadata={"outcome": outcome, "work_type": work_type},
            actor="lease_worker",
        )
        return outcome

    def heartbeat(self, work_lease_id: str) -> Mapping[str, Any]:
        return self.repository.heartbeat_agent_work(
            work_lease_id,
            self.worker_id,
            lease_seconds=self.lease_seconds,
            actor="lease_worker",
        )
