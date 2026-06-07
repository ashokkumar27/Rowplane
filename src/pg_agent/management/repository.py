"""Operational read models and management mutations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

from pg_agent.db.repository import PostgresRepository
from pg_agent.runtime.sanitize import redact_secrets


class ManagementRepository(PostgresRepository):
    """Postgres-backed management read and action repository."""

    def management_overview(self, tenant_id: str) -> Mapping[str, Any]:
        run_counts = self._count_by_status("agent_runs", tenant_id)
        task_counts = self._count_by_status("agent_tasks", tenant_id)
        pending_approvals = self._count_scalar(
            "SELECT count(*) FROM approval_requests WHERE tenant_id = %s AND status = 'pending'",
            [tenant_id],
        )
        queued_runs = int(run_counts.get("queued", 0))
        queued_tasks = int(task_counts.get("queued", 0))
        tool_counts = self._fetchone(
            """
            SELECT
              count(*)::integer AS total,
              count(*) FILTER (WHERE status = 'failed')::integer AS failed
            FROM tool_executions
            WHERE tenant_id = %s
            """,
            [tenant_id],
        ) or {"total": 0, "failed": 0}
        eval_counts = self._fetchone(
            """
            SELECT
              count(*)::integer AS total,
              count(*) FILTER (WHERE correctness = 1)::integer AS passed
            FROM eval_results
            WHERE tenant_id = %s
            """,
            [tenant_id],
        ) or {"total": 0, "passed": 0}
        recent_events = self._fetchall(
            """
            SELECT event_id, run_id, event_type, payload, actor, created_at
            FROM agent_events
            WHERE tenant_id = %s
            ORDER BY event_id DESC
            LIMIT 10
            """,
            [tenant_id],
        )
        return self._clean(
            {
                "run_status_counts": run_counts,
                "task_status_counts": task_counts,
                "pending_approvals": pending_approvals,
                "blocked_runs": int(run_counts.get("blocked", 0)),
                "queue_backlog": {
                    "runs": queued_runs,
                    "tasks": queued_tasks,
                    "total": queued_runs + queued_tasks,
                },
                "tool_failure_rate": _ratio(tool_counts["failed"], tool_counts["total"]),
                "eval_pass_rate": _ratio(eval_counts["passed"], eval_counts["total"]),
                "recent_events": recent_events,
            }
        )

    def list_management_approvals(
        self,
        tenant_id: str,
        *,
        status: str | None = "pending",
        run_id: str | None = None,
        task_id: str | None = None,
        tool_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        where = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if status:
            where.append("status = %s")
            params.append(status)
        if run_id:
            where.append("run_id = %s")
            params.append(run_id)
        if task_id:
            where.append("task_id = %s")
            params.append(task_id)
        if tool_name:
            where.append("tool_name = %s")
            params.append(tool_name)
        params.extend([limit, offset])
        return self._clean(
            self._fetchall(
                f"""
                SELECT *
                FROM management_approval_queue_v
                WHERE {' AND '.join(where)}
                ORDER BY created_at ASC, approval_request_id ASC
                LIMIT %s OFFSET %s
                """,
                params,
            )
        )

    def get_management_approval(self, approval_id: str) -> Mapping[str, Any] | None:
        approval = self._fetchone(
            "SELECT * FROM management_approval_queue_v WHERE approval_request_id = %s",
            [approval_id],
        )
        if approval is None:
            return None
        run = self.load_run(str(approval["run_id"]))
        task = self.load_task(str(approval["task_id"])) if approval.get("task_id") else None
        execution = (
            self._get_tool_execution_detail(str(approval["tool_execution_id"]))
            if approval.get("tool_execution_id")
            else None
        )
        events = self.load_events(str(approval["run_id"]), limit=50)
        return self._clean(
            {
                "approval": approval,
                "run": run,
                "task": task,
                "tool_execution": execution,
                "recent_events": events,
            }
        )

    def list_management_runs(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        agent_id: str | None = None,
        eval_case_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        where = ["rs.tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if status:
            where.append("rs.status = %s")
            params.append(status)
        if eval_case_id:
            where.append("rs.eval_case_id = %s")
            params.append(eval_case_id)
        if agent_id:
            where.append(
                "EXISTS (SELECT 1 FROM agent_tasks t WHERE t.tenant_id = rs.tenant_id AND t.run_id = rs.run_id AND t.agent_id = %s)"
            )
            params.append(agent_id)
        params.extend([limit, offset])
        return self._clean(
            self._fetchall(
                f"""
                SELECT *
                FROM management_run_summary_v rs
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC, run_id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
        )

    def get_management_run(self, run_id: str) -> Mapping[str, Any] | None:
        run = self.load_run(run_id)
        if run is None:
            return None
        tasks = self._fetchall(
            """
            SELECT t.*, a.name AS agent_name, a.role AS agent_role
            FROM agent_tasks t
            LEFT JOIN agents a ON a.tenant_id = t.tenant_id AND a.id = t.agent_id
            WHERE t.run_id = %s
            ORDER BY t.created_at, t.id
            """,
            [run_id],
        )
        messages = self._fetchall(
            """
            SELECT * FROM agent_messages
            WHERE run_id = %s
            ORDER BY created_at, id
            """,
            [run_id],
        )
        approvals = self._fetchall(
            """
            SELECT * FROM management_approval_queue_v
            WHERE run_id = %s
            ORDER BY created_at, approval_request_id
            """,
            [run_id],
        )
        tools = self._fetchall(
            """
            SELECT te.*, at.name AS tool_name
            FROM tool_executions te
            JOIN agent_tools at ON at.tenant_id = te.tenant_id AND at.id = te.tool_id
            WHERE te.run_id = %s
            ORDER BY te.created_at, te.id
            """,
            [run_id],
        )
        return self._clean(
            {
                "run": run,
                "tasks": tasks,
                "messages": messages,
                "approvals": approvals,
                "tool_executions": tools,
            }
        )

    def get_management_run_timeline(self, run_id: str, *, limit: int = 300) -> list[Mapping[str, Any]]:
        items: list[dict[str, Any]] = []
        for event in self.load_events(run_id, limit=limit):
            items.append(
                {
                    "kind": "event",
                    "type": event["event_type"],
                    "created_at": event["created_at"],
                    "data": event,
                }
            )
        for message in self._fetchall(
            "SELECT * FROM agent_messages WHERE run_id = %s ORDER BY created_at, id LIMIT %s",
            [run_id, limit],
        ):
            items.append(
                {
                    "kind": "message",
                    "type": message["message_type"],
                    "created_at": message["created_at"],
                    "data": message,
                }
            )
        for approval in self._fetchall(
            "SELECT * FROM management_approval_queue_v WHERE run_id = %s ORDER BY created_at LIMIT %s",
            [run_id, limit],
        ):
            items.append(
                {
                    "kind": "approval",
                    "type": str(approval["status"]),
                    "created_at": approval["created_at"],
                    "data": approval,
                }
            )
        for execution in self._fetchall(
            """
            SELECT te.*, at.name AS tool_name
            FROM tool_executions te
            JOIN agent_tools at ON at.tenant_id = te.tenant_id AND at.id = te.tool_id
            WHERE te.run_id = %s
            ORDER BY te.created_at
            LIMIT %s
            """,
            [run_id, limit],
        ):
            items.append(
                {
                    "kind": "tool_execution",
                    "type": str(execution["status"]),
                    "created_at": execution["created_at"],
                    "data": execution,
                }
            )
        return self._clean(sorted(items, key=lambda item: str(item["created_at"])))

    def get_management_run_trajectory(
        self,
        tenant_id: str,
        run_id: str,
        *,
        limit: int = 500,
    ) -> list[Mapping[str, Any]]:
        return self._clean(self.list_run_trajectory(tenant_id, run_id, limit=limit))

    def search_management_harness(
        self,
        tenant_id: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[Mapping[str, Any]]:
        if not query.strip():
            return []
        return self._clean(self.search_harness(tenant_id, query, limit=limit))

    def retry_management_run(self, tenant_id: str, run_id: str) -> Mapping[str, Any]:
        source = self.load_run(run_id)
        if source is None:
            raise LookupError(f"run not found: {run_id}")
        if str(source["status"]) not in {"failed", "blocked"}:
            raise ValueError("only failed or blocked runs can be retried")
        new_run_id = str(uuid4())
        retry = self.create_run(
            tenant_id,
            new_run_id,
            source["task"],
            model=str(source.get("model") or "unset"),
            max_iterations=int(source.get("max_iterations") or 8),
        )
        self.append_event(
            tenant_id,
            run_id,
            "retry_created",
            {"new_run_id": new_run_id},
            actor="management_api",
        )
        self.append_event(
            tenant_id,
            new_run_id,
            "run_retried_from",
            {"source_run_id": run_id},
            actor="management_api",
        )
        self.queue_run(tenant_id, new_run_id)
        return self._clean(retry)

    def list_management_tools(self, tenant_id: str) -> list[Mapping[str, Any]]:
        return self._clean(
            self._fetchall(
                """
                SELECT th.*, tools.description, tools.input_schema, tools.output_schema, tools.approval_policy
                FROM management_tool_health_v th
                JOIN agent_tools tools ON tools.tenant_id = th.tenant_id AND tools.id = th.tool_id
                WHERE th.tenant_id = %s
                ORDER BY th.tool_name
                """,
                [tenant_id],
            )
        )

    def append_audit_event(
        self,
        tenant_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "management_api",
    ) -> None:
        self._execute(
            """
            INSERT INTO audit_events (tenant_id, event_type, payload, actor)
            VALUES (%s, %s, %s::jsonb, %s)
            """,
            [tenant_id, event_type, self._json(payload), actor],
        )

    def update_management_tool(
        self,
        tool_id: str,
        *,
        enabled: bool | None = None,
        requires_approval: bool | None = None,
        description: str | None = None,
        actor: str = "management_api",
    ) -> Mapping[str, Any]:
        assignments: list[str] = []
        params: list[Any] = []
        if enabled is not None:
            assignments.append("enabled = %s")
            params.append(enabled)
        if requires_approval is not None:
            assignments.append("requires_approval = %s")
            params.append(requires_approval)
        if description is not None:
            assignments.append("description = %s")
            params.append(description)
        if not assignments:
            row = self._fetchone("SELECT * FROM agent_tools WHERE id = %s", [tool_id])
            if row is None:
                raise LookupError(f"tool not found: {tool_id}")
            return self._clean(row)

        params.append(tool_id)
        row = self._fetchone(
            f"UPDATE agent_tools SET {', '.join(assignments)} WHERE id = %s RETURNING *",
            params,
        )
        if row is None:
            raise LookupError(f"tool not found: {tool_id}")
        self.append_audit_event(
            str(row["tenant_id"]),
            "tool_updated",
            {
                "tool_id": tool_id,
                "enabled": enabled,
                "requires_approval": requires_approval,
                "description_changed": description is not None,
            },
            actor=actor,
        )
        return self._clean(row)

    def list_management_agents(self, tenant_id: str) -> list[Mapping[str, Any]]:
        return self._clean(
            self._fetchall(
                """
                SELECT
                  a.*,
                  count(t.id)::integer AS task_count,
                  count(t.id) FILTER (WHERE t.status = 'failed')::integer AS failed_task_count,
                  count(t.id) FILTER (WHERE t.status = 'blocked')::integer AS blocked_task_count
                FROM agents a
                LEFT JOIN agent_tasks t ON t.tenant_id = a.tenant_id AND t.agent_id = a.id
                WHERE a.tenant_id = %s
                GROUP BY a.id
                ORDER BY a.name
                """,
                [tenant_id],
            )
        )

    def get_management_agent(self, agent_id: str) -> Mapping[str, Any] | None:
        agent = self.load_agent(agent_id)
        if agent is None:
            return None
        permissions = self._fetchall(
            """
            SELECT p.*, t.name AS tool_name
            FROM agent_tool_permissions p
            JOIN agent_tools t ON t.tenant_id = p.tenant_id AND t.id = p.tool_id
            WHERE p.subject_type = 'agent' AND p.subject_id = %s
            ORDER BY t.name
            """,
            [agent_id],
        )
        task_counts = self._fetchall(
            """
            SELECT status, count(*)::integer AS count
            FROM agent_tasks
            WHERE agent_id = %s
            GROUP BY status
            ORDER BY status
            """,
            [agent_id],
        )
        return self._clean(
            {
                "agent": agent,
                "tool_permissions": permissions,
                "task_status_counts": {str(row["status"]): int(row["count"]) for row in task_counts},
            }
        )

    def list_management_eval_summaries(self, tenant_id: str) -> list[Mapping[str, Any]]:
        return self._clean(
            self._fetchall(
                "SELECT * FROM management_eval_summary_v WHERE tenant_id = %s ORDER BY eval_case_name",
                [tenant_id],
            )
        )

    def list_management_eval_results(
        self,
        tenant_id: str,
        eval_case_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        return self._clean(
            self._fetchall(
                """
                SELECT er.*, ec.name AS eval_case_name, r.status AS run_status
                FROM eval_results er
                JOIN eval_cases ec ON ec.tenant_id = er.tenant_id AND ec.id = er.eval_case_id
                JOIN agent_runs r ON r.tenant_id = er.tenant_id AND r.id = er.run_id
                WHERE er.tenant_id = %s AND er.eval_case_id = %s
                ORDER BY er.created_at DESC, er.id DESC
                LIMIT %s OFFSET %s
                """,
                [tenant_id, eval_case_id, limit, offset],
            )
        )

    def list_management_audit_events(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        where = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if event_type:
            where.append("event_type = %s")
            params.append(event_type)
        if actor:
            where.append("actor = %s")
            params.append(actor)
        if run_id:
            where.append("run_id = %s")
            params.append(run_id)
            params.extend([limit, offset])
            return self._clean(
                self._fetchall(
                    f"""
                    SELECT 'agent_event' AS source, event_id, tenant_id, run_id, event_type, payload, actor, created_at
                    FROM agent_events
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC, event_id DESC
                    LIMIT %s OFFSET %s
                    """,
                    params,
                )
            )

        event_where = ' AND '.join(where)
        params.extend([tenant_id, *params[1:], limit, offset])
        return self._clean(
            self._fetchall(
                f"""
                SELECT * FROM (
                  SELECT 'agent_event' AS source, event_id, tenant_id, run_id, event_type, payload, actor, created_at
                  FROM agent_events
                  WHERE {event_where}
                  UNION ALL
                  SELECT 'audit_event' AS source, event_id, tenant_id, NULL::uuid AS run_id, event_type, payload, actor, created_at
                  FROM audit_events
                  WHERE {event_where}
                ) events
                ORDER BY created_at DESC, event_id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
        )

    def list_management_memory(
        self,
        tenant_id: str,
        *,
        memory_type: str | None = None,
        source_run_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Mapping[str, Any]]:
        where = ["tenant_id = %s"]
        params: list[Any] = [tenant_id]
        if memory_type:
            where.append("memory_type = %s")
            params.append(memory_type)
        if source_run_id:
            where.append("source_run_id = %s")
            params.append(source_run_id)
        params.extend([limit, offset])
        return self._clean(
            self._fetchall(
                f"""
                SELECT id, tenant_id, memory_type, content, metadata, source_run_id, created_at
                FROM agent_memory
                WHERE {' AND '.join(where)}
                ORDER BY created_at DESC, id DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
        )

    def _count_by_status(self, table: str, tenant_id: str) -> dict[str, int]:
        rows = self._fetchall(
            f"SELECT status, count(*)::integer AS count FROM {table} WHERE tenant_id = %s GROUP BY status",
            [tenant_id],
        )
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _count_scalar(self, sql: str, params: Sequence[Any]) -> int:
        row = self._fetchone(sql, params)
        if row is None:
            return 0
        return int(next(iter(row.values())))

    def _get_tool_execution_detail(self, execution_id: str) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT te.*, at.name AS tool_name
            FROM tool_executions te
            JOIN agent_tools at ON at.tenant_id = te.tenant_id AND at.id = te.tool_id
            WHERE te.id = %s
            """,
            [execution_id],
        )

    def _clean(self, value: Any) -> Any:
        return redact_secrets(_coerce_numbers(value))


def _ratio(numerator: Any, denominator: Any) -> float | None:
    denominator_value = int(denominator or 0)
    if denominator_value == 0:
        return None
    return float(numerator or 0) / denominator_value


def _coerce_numbers(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {key: _coerce_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_coerce_numbers(item) for item in value]
    return value
