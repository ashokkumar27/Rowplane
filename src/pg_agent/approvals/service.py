"""Approval workflow service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from pg_agent.runtime.errors import ApprovalAlreadyResolved, ApprovalNotFound


class ApprovalRepository(Protocol):
    def get_approval_request(self, approval_id: str) -> Mapping[str, Any] | None: ...

    def resolve_approval_request(
        self,
        approval_id: str,
        status: str,
        resolved_by: str,
    ) -> Mapping[str, Any]: ...

    def load_run(self, run_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None: ...

    def load_task(self, task_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None: ...

    def append_event(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "worker",
    ) -> None: ...

    def update_run_status(
        self,
        run_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]: ...

    def update_task_status(
        self,
        task_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]: ...

    def queue_run(self, tenant_id: str, run_id: str) -> None: ...

    def queue_task(self, tenant_id: str, run_id: str, task_id: str) -> None: ...

    def create_agent_message(
        self,
        tenant_id: str,
        run_id: str,
        message_type: str,
        content: Mapping[str, Any],
        *,
        from_task_id: str | None = None,
        to_task_id: str | None = None,
    ) -> Mapping[str, Any]: ...


class ApprovalService:
    def __init__(self, repository: ApprovalRepository) -> None:
        self.repository = repository

    def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        resolved_by: str,
    ) -> Mapping[str, Any]:
        approval = self.repository.get_approval_request(approval_id)
        if approval is None:
            raise ApprovalNotFound(f"approval request not found: {approval_id}")
        if approval.get("status") != "pending":
            raise ApprovalAlreadyResolved(f"approval request is already {approval['status']}")

        status = "approved" if approved else "rejected"
        resolved = self.repository.resolve_approval_request(
            approval_id,
            status,
            resolved_by,
        )
        tenant_id = str(resolved["tenant_id"])
        run_id = str(resolved["run_id"])
        task_id = str(resolved["task_id"]) if resolved.get("task_id") else None
        run = self.repository.load_run(run_id, for_update=True)
        if run is None:
            raise ApprovalNotFound(f"run not found for approval request: {approval_id}")

        payload: dict[str, Any] = {
            "approval_request_id": approval_id,
            "status": status,
            "resolved_by": resolved_by,
        }
        if task_id is not None:
            payload["task_id"] = task_id
        self.repository.append_event(
            tenant_id,
            run_id,
            "approval_resolved",
            payload,
            actor=resolved_by,
        )
        if task_id is not None:
            task = self.repository.load_task(task_id, for_update=True)
            if task is None:
                raise ApprovalNotFound(f"task not found for approval request: {approval_id}")
            if approved:
                self.repository.update_task_status(task_id, str(task["status"]), "queued")
                self.repository.queue_task(tenant_id, run_id, task_id)
            else:
                blocked_task = self.repository.update_task_status(
                    task_id,
                    str(task["status"]),
                    "blocked",
                    error=f"approval request {approval_id} was rejected",
                )
                parent_task_id = blocked_task.get("parent_task_id")
                if parent_task_id:
                    self.repository.create_agent_message(
                        tenant_id,
                        run_id,
                        "task_result",
                        {
                            "child_task_id": task_id,
                            "status": "blocked",
                            "error": blocked_task.get("error"),
                        },
                        from_task_id=task_id,
                        to_task_id=str(parent_task_id),
                    )
                    self.repository.append_event(
                        tenant_id,
                        run_id,
                        "task_result_reported",
                        {
                            "parent_task_id": str(parent_task_id),
                            "child_task_id": task_id,
                            "status": "blocked",
                        },
                    )
                    parent = self.repository.load_task(str(parent_task_id), for_update=True)
                    if parent is not None and str(parent["status"]) == "waiting_child":
                        self.repository.update_task_status(
                            str(parent_task_id),
                            "waiting_child",
                            "queued",
                        )
                        self.repository.queue_task(tenant_id, run_id, str(parent_task_id))
            return resolved

        if approved:
            self.repository.update_run_status(run_id, str(run["status"]), "queued")
            self.repository.queue_run(tenant_id, run_id)
        else:
            self.repository.update_run_status(
                run_id,
                str(run["status"]),
                "blocked",
                error=f"approval request {approval_id} was rejected",
            )
        return resolved
