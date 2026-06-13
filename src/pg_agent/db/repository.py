"""Explicit SQL repository for the Postgres control plane."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pg_agent.memory.repository import MemorySearch, build_memory_where, vector_literal
from pg_agent.runtime.errors import ApprovalAlreadyResolved, RunStatusConflict
from pg_agent.runtime.sanitize import redact_secrets
from pg_agent.runtime.states import TERMINAL_STATUSES, validate_transition


class PostgresRepository:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.conn.transaction():
            yield

    def set_tenant(self, tenant_id: str) -> None:
        self._execute("SELECT set_config('app.tenant_id', %s, false)", [tenant_id])

    def load_run(self, run_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        return self._fetchone(f"SELECT * FROM agent_runs WHERE id = %s{suffix}", [run_id])


    def create_run(
        self,
        tenant_id: str,
        run_id: str,
        task: Mapping[str, Any],
        *,
        model: str = "sample-scripted-model",
        max_iterations: int = 8,
        required_capabilities: Sequence[str] | None = None,
        priority: int = 0,
        not_before: Any = None,
        deadline_at: Any = None,
    ) -> Mapping[str, Any]:
        return self._fetchone(
            """
            INSERT INTO agent_runs (
              id, tenant_id, task, model, max_iterations, required_capabilities, priority, not_before, deadline_at
            )
            VALUES (%s, %s, %s::jsonb, %s, %s, %s::text[], %s, %s, %s)
            RETURNING *
            """,
            [
                run_id,
                tenant_id,
                self._json(task),
                model,
                max_iterations,
                list(required_capabilities or []),
                priority,
                not_before,
                deadline_at,
            ],
        )

    def get_eval_case_id(self, tenant_id: str, name: str) -> str:
        row = self._fetchone(
            """
            SELECT id FROM eval_cases
            WHERE tenant_id = %s AND name = %s
            """,
            [tenant_id, name],
        )
        if row is None:
            raise RuntimeError(f"eval case not found: {name}")
        return str(row["id"])

    def pending_approval_for_run(self, run_id: str) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM approval_requests
            WHERE run_id = %s AND status = 'pending'
            ORDER BY created_at
            LIMIT 1
            """,
            [run_id],
        )

    def list_tool_executions(self, run_id: str) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT
              te.*,
              at.name AS tool_name,
              at.description AS tool_description,
              at.is_side_effecting AS tool_is_side_effecting,
              at.requires_approval AS tool_requires_approval,
              at.input_schema AS tool_input_schema,
              at.output_schema AS tool_output_schema,
              at.approval_policy AS tool_approval_policy
            FROM tool_executions te
            LEFT JOIN agent_tools at ON at.tenant_id = te.tenant_id AND at.id = te.tool_id
            WHERE te.run_id = %s
            ORDER BY te.created_at, te.id
            """,
            [run_id],
        )

    def get_agent_by_name(self, tenant_id: str, name: str) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM agents
            WHERE tenant_id = %s AND name = %s AND enabled = true
            """,
            [tenant_id, name],
        )

    def load_agent(self, agent_id: str) -> Mapping[str, Any] | None:
        return self._fetchone("SELECT * FROM agents WHERE id = %s", [agent_id])

    def create_agent_task(
        self,
        tenant_id: str,
        run_id: str,
        agent_id: str,
        task_input: Mapping[str, Any],
        *,
        parent_task_id: str | None = None,
        max_iterations: int = 10,
        required_capabilities: Sequence[str] | None = None,
        priority: int = 0,
        not_before: Any = None,
        deadline_at: Any = None,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO agent_tasks (
              tenant_id, run_id, agent_id, parent_task_id, input, max_iterations,
              required_capabilities, priority, not_before, deadline_at
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::text[], %s, %s, %s)
            RETURNING *
            """,
            [
                tenant_id,
                run_id,
                agent_id,
                parent_task_id,
                self._json(task_input),
                max_iterations,
                list(required_capabilities or []),
                priority,
                not_before,
                deadline_at,
            ],
        )
        if row is None:
            raise RuntimeError("agent task was not created")
        return row

    def load_task(self, task_id: str, *, for_update: bool = False) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        return self._fetchone(f"SELECT * FROM agent_tasks WHERE id = %s{suffix}", [task_id])

    def update_task_status(
        self,
        task_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        from pg_agent.runtime.task_states import TERMINAL_TASK_STATUSES, validate_task_transition

        validate_task_transition(current_status, next_status)
        assignments = ["status = %s"]
        params: list[Any] = [next_status]
        if "output" in fields:
            assignments.append("output = %s::jsonb")
            params.append(self._json(fields["output"]))
        if "error" in fields:
            assignments.append("error = %s")
            params.append(fields["error"])
        if next_status in {str(status) for status in TERMINAL_TASK_STATUSES}:
            assignments.append("completed_at = COALESCE(completed_at, now())")
        params.extend([task_id, current_status])
        row = self._fetchone(
            f"""
            UPDATE agent_tasks
            SET {', '.join(assignments)}
            WHERE id = %s AND status = %s
            RETURNING *
            """,
            params,
        )
        if row is None:
            raise RunStatusConflict(
                f"task {task_id} was not in expected status {current_status}"
            )
        return row

    def increment_task_iteration(self, task_id: str) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            UPDATE agent_tasks
            SET iteration_count = iteration_count + 1
            WHERE id = %s
            RETURNING *
            """,
            [task_id],
        )
        if row is None:
            raise RunStatusConflict(f"task not found while incrementing iteration: {task_id}")
        return row

    def create_agent_message(
        self,
        tenant_id: str,
        run_id: str,
        message_type: str,
        content: Mapping[str, Any],
        *,
        from_task_id: str | None = None,
        to_task_id: str | None = None,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO agent_messages (
              tenant_id, run_id, from_task_id, to_task_id, message_type, content
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """,
            [tenant_id, run_id, from_task_id, to_task_id, message_type, self._json(content)],
        )
        if row is None:
            raise RuntimeError("agent message was not created")
        return row

    def reserve_model_call(
        self,
        tenant_id: str,
        run_id: str,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        model: str = "unset",
        actor: str = "worker",
        projected_cost_usd: float | None = None,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.reserve_model_call(
              %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s::numeric
            ) AS decision
            """,
            [tenant_id, run_id, task_id, agent_id, model, actor, projected_cost_usd or 0],
        )
        return self._json_result(row, "decision")

    def complete_model_call(
        self,
        tenant_id: str,
        run_id: str,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        model: str = "unset",
        status: str = "completed",
        latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        estimated_cost_usd: float | None = None,
        error: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.complete_model_call(
              %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s::numeric, %s, %s
            ) AS decision
            """,
            [
                tenant_id,
                run_id,
                task_id,
                agent_id,
                model,
                status,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                estimated_cost_usd,
                error,
                actor,
            ],
        )
        return self._json_result(row, "decision")

    def runtime_budget_allows(
        self,
        tenant_id: str,
        metric: str,
        *,
        increment: int = 1,
        run_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.runtime_budget_allows(
              %s::uuid, %s, %s, %s::uuid, %s::uuid, %s::uuid, %s
            ) AS decision
            """,
            [tenant_id, metric, increment, run_id, task_id, agent_id, actor],
        )
        return self._json_result(row, "decision")

    def upsert_tenant_budget(
        self,
        tenant_id: str,
        *,
        max_model_calls: int | None = None,
        max_tool_executions: int | None = None,
        max_child_tasks: int | None = None,
        max_active_work: int | None = None,
        max_estimated_cost_usd: float | None = None,
        enabled: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO agent_runtime_budgets (
              tenant_id, scope_type, scope_id, max_model_calls, max_tool_executions,
              max_child_tasks, max_active_work, max_estimated_cost_usd, metadata, enabled
            )
            VALUES (%s, 'tenant', %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (tenant_id, scope_type, scope_id) DO UPDATE SET
              max_model_calls = EXCLUDED.max_model_calls,
              max_tool_executions = EXCLUDED.max_tool_executions,
              max_child_tasks = EXCLUDED.max_child_tasks,
              max_active_work = EXCLUDED.max_active_work,
              max_estimated_cost_usd = EXCLUDED.max_estimated_cost_usd,
              metadata = EXCLUDED.metadata,
              enabled = EXCLUDED.enabled,
              updated_at = now()
            RETURNING *
            """,
            [
                tenant_id,
                tenant_id,
                max_model_calls,
                max_tool_executions,
                max_child_tasks,
                max_active_work,
                max_estimated_cost_usd,
                self._json(metadata or {}),
                enabled,
            ],
        )
        if row is None:
            raise RuntimeError("tenant budget was not saved")
        return row

    def get_tenant_budget(self, tenant_id: str) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM agent_runtime_budgets
            WHERE tenant_id = %s AND scope_type = 'tenant' AND scope_id = %s
            """,
            [tenant_id, tenant_id],
        )

    def create_task_dependency(
        self,
        tenant_id: str,
        run_id: str,
        parent_task_id: str,
        child_task_id: str,
        *,
        dependency_type: str = "completion",
        required: bool = True,
        metadata: Mapping[str, Any] | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.create_task_dependency(
              %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s, %s::jsonb, %s
            ) AS decision
            """,
            [
                tenant_id,
                run_id,
                parent_task_id,
                child_task_id,
                dependency_type,
                required,
                self._json(metadata or {}),
                actor,
            ],
        )
        return self._json_result(row, "decision")

    def complete_task_dependencies_for_child(
        self,
        tenant_id: str,
        run_id: str,
        child_task_id: str,
        child_status: str,
        *,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.complete_task_dependencies_for_child(
              %s::uuid, %s::uuid, %s::uuid, %s, %s
            ) AS decision
            """,
            [tenant_id, run_id, child_task_id, child_status, actor],
        )
        return self._json_result(row, "decision")

    def load_task_messages(
        self,
        run_id: str,
        task_id: str,
        *,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT * FROM agent_messages
            WHERE run_id = %s
              AND (to_task_id = %s OR from_task_id = %s OR to_task_id IS NULL)
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            [run_id, task_id, task_id, limit],
        )[::-1]

    def queue_task(self, tenant_id: str, run_id: str, task_id: str) -> None:
        self._execute(
            "SELECT pgmq.send('agent_wakeups', %s::jsonb)",
            [self._json({"tenant_id": tenant_id, "run_id": run_id, "task_id": task_id})],
        )

    def pending_approval_for_task(self, task_id: str) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM approval_requests
            WHERE task_id = %s AND status = 'pending'
            ORDER BY created_at
            LIMIT 1
            """,
            [task_id],
        )

    def load_events(self, run_id: str, *, limit: int = 200) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT event_id, tenant_id, run_id, event_type, payload, actor, created_at
            FROM agent_events
            WHERE run_id = %s
            ORDER BY event_id DESC
            LIMIT %s
            """,
            [run_id, limit],
        )[::-1]

    def append_event(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "worker",
    ) -> None:
        self._execute(
            """
            INSERT INTO agent_events (tenant_id, run_id, event_type, payload, actor)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            """,
            [tenant_id, run_id, event_type, self._json(payload), actor],
        )

    def update_run_status(
        self,
        run_id: str,
        current_status: str,
        next_status: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        validate_transition(current_status, next_status)
        assignments = ["status = %s"]
        params: list[Any] = [next_status]
        if "answer" in fields:
            assignments.append("answer = %s::jsonb")
            params.append(self._json(fields["answer"]))
        if "error" in fields:
            assignments.append("error = %s")
            params.append(fields["error"])
        if next_status in {str(status) for status in TERMINAL_STATUSES}:
            assignments.append("completed_at = COALESCE(completed_at, now())")
        params.extend([run_id, current_status])
        row = self._fetchone(
            f"""
            UPDATE agent_runs
            SET {', '.join(assignments)}
            WHERE id = %s AND status = %s
            RETURNING *
            """,
            params,
        )
        if row is None:
            raise RunStatusConflict(
                f"run {run_id} was not in expected status {current_status}"
            )
        return row

    def increment_iteration(self, run_id: str) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            UPDATE agent_runs
            SET iteration_count = iteration_count + 1
            WHERE id = %s
            RETURNING *
            """,
            [run_id],
        )
        if row is None:
            raise RunStatusConflict(f"run not found while incrementing iteration: {run_id}")
        return row

    def queue_run(self, tenant_id: str, run_id: str) -> None:
        self._execute(
            "SELECT pgmq.send('agent_wakeups', %s::jsonb)",
            [self._json({"tenant_id": tenant_id, "run_id": run_id})],
        )

    def read_queue_message(self, *, visibility_timeout_seconds: int = 30) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT msg_id, read_ct, enqueued_at, vt, message
            FROM pgmq.read('agent_wakeups', %s, 1)
            """,
            [visibility_timeout_seconds],
        )

    def delete_queue_message(self, msg_id: int) -> None:
        self._execute("SELECT pgmq.delete('agent_wakeups', %s)", [msg_id])

    def submit_agent_command(
        self,
        tenant_id: str,
        run_id: str,
        command: Mapping[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.submit_agent_command(
              %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::jsonb, %s
            ) AS decision
            """,
            [tenant_id, run_id, task_id, agent_id, self._json(command), actor],
        )
        return self._json_result(row, "decision")

    def simulate_agent_intent_policy(
        self,
        tenant_id: str,
        run_id: str,
        intent: Mapping[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.simulate_agent_intent_policy(
              %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::jsonb, %s
            ) AS decision
            """,
            [tenant_id, run_id, task_id, agent_id, self._json(intent), actor],
        )
        return self._json_result(row, "decision")

    def submit_agent_intent(
        self,
        tenant_id: str,
        run_id: str,
        intent: Mapping[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.submit_agent_intent(
              %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s::jsonb, %s
            ) AS decision
            """,
            [tenant_id, run_id, task_id, agent_id, self._json(intent), actor],
        )
        return self._json_result(row, "decision")

    def reserve_tool_execution(
        self,
        tenant_id: str,
        run_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        force_approval: bool = False,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.reserve_tool_execution(
              %s::uuid, %s::uuid, %s::uuid, %s::uuid, %s, %s::jsonb, %s, %s
            ) AS decision
            """,
            [
                tenant_id,
                run_id,
                task_id,
                agent_id,
                tool_name,
                self._json(arguments),
                force_approval,
                actor,
            ],
        )
        return self._json_result(row, "decision")

    def complete_tool_execution(
        self,
        execution_id: str,
        *,
        succeeded: bool,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
        actor: str = "worker",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.complete_tool_execution(
              %s::uuid, %s, %s::jsonb, %s, %s
            ) AS decision
            """,
            [execution_id, succeeded, self._json(result or {}), error, actor],
        )
        return self._json_result(row, "decision")

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
    ) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT work_lease_id, tenant_id, run_id, task_id, work_type, lease_expires_at, payload
            FROM app.claim_agent_work(
              %s::uuid,
              %s,
              %s::text[],
              %s,
              %s,
              %s::text[],
              %s
            )
            """,
            [
                tenant_id,
                worker_id,
                list(capabilities or []),
                max_items,
                lease_seconds,
                list(kinds or ["task", "run"]),
                actor,
            ],
        )

    def heartbeat_agent_work(
        self,
        work_lease_id: str,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        actor: str = "scheduler",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.heartbeat_agent_work(%s::uuid, %s, %s, %s) AS decision
            """,
            [work_lease_id, worker_id, lease_seconds, actor],
        )
        return self._json_result(row, "decision")

    def complete_agent_work(
        self,
        work_lease_id: str,
        worker_id: str,
        *,
        status: str = "completed",
        metadata: Mapping[str, Any] | None = None,
        actor: str = "scheduler",
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            SELECT app.complete_agent_work(%s::uuid, %s, %s, %s::jsonb, %s) AS decision
            """,
            [work_lease_id, worker_id, status, self._json(metadata or {}), actor],
        )
        return self._json_result(row, "decision")

    def list_run_trajectory(
        self,
        tenant_id: str,
        run_id: str,
        *,
        limit: int = 500,
    ) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT source, sequence_id, created_at, step_type, actor, payload
            FROM app.run_trajectory_v
            WHERE tenant_id = %s AND run_id = %s
            ORDER BY created_at, sequence_id
            LIMIT %s
            """,
            [tenant_id, run_id, limit],
        )

    def search_harness(
        self,
        tenant_id: str,
        query: str,
        *,
        limit: int = 50,
    ) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT source, id, run_id, created_at, rank, snippet, payload
            FROM app.search_harness(%s::uuid, %s, %s)
            """,
            [tenant_id, query, limit],
        )

    def upsert_tool(
        self,
        tenant_id: str,
        name: str,
        *,
        description: str = "",
        input_schema: Mapping[str, Any] | None = None,
        output_schema: Mapping[str, Any] | None = None,
        is_side_effecting: bool = False,
        requires_approval: bool = False,
        approval_policy: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO agent_tools (
              tenant_id, name, description, input_schema, output_schema, is_side_effecting, requires_approval, approval_policy, enabled
            )
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s)
            ON CONFLICT (tenant_id, name)
            DO UPDATE SET
              description = EXCLUDED.description,
              input_schema = EXCLUDED.input_schema,
              output_schema = EXCLUDED.output_schema,
              is_side_effecting = EXCLUDED.is_side_effecting,
              requires_approval = EXCLUDED.requires_approval,
              approval_policy = EXCLUDED.approval_policy,
              enabled = EXCLUDED.enabled
            RETURNING *
            """,
            [
                tenant_id,
                name,
                description,
                self._json(input_schema or {"type": "object"}),
                self._json(output_schema or {"type": "object"}),
                is_side_effecting,
                requires_approval,
                self._json(approval_policy or {}),
                enabled,
            ],
        )
        if row is None:
            raise RuntimeError(f"tool was not upserted: {name}")
        return row

    def grant_tool_permission(
        self,
        tenant_id: str,
        tool_id: str,
        subject_type: str,
        subject_id: str,
        *,
        allowed: bool = True,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO agent_tool_permissions (tenant_id, tool_id, subject_type, subject_id, allowed)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, tool_id, subject_type, subject_id)
            DO UPDATE SET allowed = EXCLUDED.allowed
            RETURNING *
            """,
            [tenant_id, tool_id, subject_type, subject_id, allowed],
        )
        if row is None:
            raise RuntimeError(f"tool permission was not upserted: {tool_id}")
        return row

    def upsert_agent(
        self,
        tenant_id: str,
        name: str,
        *,
        role: str,
        instructions: str,
        model: str = "unset",
        enabled: bool = True,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO agents (tenant_id, name, role, instructions, model, enabled)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, name)
            DO UPDATE SET
              role = EXCLUDED.role,
              instructions = EXCLUDED.instructions,
              model = EXCLUDED.model,
              enabled = EXCLUDED.enabled
            RETURNING *
            """,
            [tenant_id, name, role, instructions, model, enabled],
        )
        if row is None:
            raise RuntimeError(f"agent was not upserted: {name}")
        return row

    def get_agent_tool(self, tenant_id: str, tool_name: str) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM agent_tools
            WHERE tenant_id = %s AND name = %s
            """,
            [tenant_id, tool_name],
        )

    def list_approval_requests(self, run_id: str) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT * FROM approval_requests
            WHERE run_id = %s
            ORDER BY created_at, id
            """,
            [run_id],
        )

    def has_tool_permission(
        self,
        tenant_id: str,
        tool_id: str,
        run_id: str,
        *,
        agent_id: str | None = None,
    ) -> bool:
        subjects_sql = ["(subject_type = 'run' AND subject_id = %s)"]
        params: list[Any] = [tenant_id, tool_id, run_id]
        if agent_id is not None:
            subjects_sql.append("(subject_type = 'agent' AND subject_id = %s)")
            params.append(agent_id)
        subjects_sql.append("(subject_type = 'tenant' AND subject_id = %s)")
        params.append(tenant_id)
        row = self._fetchone(
            f"""
            SELECT allowed
            FROM agent_tool_permissions
            WHERE tenant_id = %s
              AND tool_id = %s
              AND ({' OR '.join(subjects_sql)})
            ORDER BY CASE subject_type
              WHEN 'run' THEN 0
              WHEN 'agent' THEN 1
              WHEN 'tenant' THEN 2
              ELSE 3
            END
            LIMIT 1
            """,
            params,
        )
        return bool(row and row["allowed"])

    def get_tool_execution_by_key(
        self,
        tenant_id: str,
        tool_id: str,
        idempotency_key: str,
    ) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM tool_executions
            WHERE tenant_id = %s AND tool_id = %s AND idempotency_key = %s
            """,
            [tenant_id, tool_id, idempotency_key],
        )

    def create_tool_execution(
        self,
        tenant_id: str,
        run_id: str,
        tool_id: str,
        idempotency_key: str,
        arguments: Mapping[str, Any],
        arguments_hash: str,
        *,
        task_id: str | None = None,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            INSERT INTO tool_executions (
              tenant_id, run_id, task_id, tool_id, idempotency_key, arguments, arguments_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (tenant_id, tool_id, idempotency_key) DO NOTHING
            RETURNING *
            """,
            [tenant_id, run_id, task_id, tool_id, idempotency_key, self._json(arguments), arguments_hash],
        )
        if row is not None:
            return row
        existing = self.get_tool_execution_by_key(tenant_id, tool_id, idempotency_key)
        if existing is None:
            raise RuntimeError("tool execution conflict could not be loaded")
        return existing

    def update_tool_execution(
        self,
        execution_id: str,
        status: str,
        **fields: Any,
    ) -> Mapping[str, Any]:
        assignments = ["status = %s"]
        params: list[Any] = [status]
        if status == "running":
            assignments.append("started_at = COALESCE(started_at, now())")
        if status in {"completed", "failed"}:
            assignments.append("completed_at = COALESCE(completed_at, now())")
        if "result" in fields:
            assignments.append("result = %s::jsonb")
            params.append(self._json(fields["result"]))
        if "error" in fields:
            assignments.append("error = %s")
            params.append(fields["error"])
        params.append(execution_id)
        row = self._fetchone(
            f"UPDATE tool_executions SET {', '.join(assignments)} WHERE id = %s RETURNING *",
            params,
        )
        if row is None:
            raise RuntimeError(f"tool execution not found: {execution_id}")
        return row

    def create_approval_request(
        self,
        tenant_id: str,
        run_id: str,
        reason: str,
        payload: Mapping[str, Any],
        *,
        tool_execution_id: str | None = None,
        task_id: str | None = None,
    ) -> Mapping[str, Any]:
        return self._fetchone(
            """
            INSERT INTO approval_requests (tenant_id, run_id, task_id, tool_execution_id, reason, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """,
            [tenant_id, run_id, task_id, tool_execution_id, reason, self._json(payload)],
        )

    def get_approval_for_execution(self, execution_id: str) -> Mapping[str, Any] | None:
        return self._fetchone(
            """
            SELECT * FROM approval_requests
            WHERE tool_execution_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            [execution_id],
        )

    def get_approval_request(self, approval_id: str) -> Mapping[str, Any] | None:
        return self._fetchone("SELECT * FROM approval_requests WHERE id = %s", [approval_id])

    def resolve_approval_request(
        self,
        approval_id: str,
        status: str,
        resolved_by: str,
    ) -> Mapping[str, Any]:
        row = self._fetchone(
            """
            UPDATE approval_requests
            SET status = %s, resolved_by = %s, resolved_at = now()
            WHERE id = %s AND status = 'pending'
            RETURNING *
            """,
            [status, resolved_by, approval_id],
        )
        if row is None:
            raise ApprovalAlreadyResolved(
                f"approval request is not pending or does not exist: {approval_id}"
            )
        return row

    def create_memory(
        self,
        tenant_id: str,
        memory_type: str,
        content: str,
        metadata: Mapping[str, Any],
        *,
        source_run_id: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> Mapping[str, Any]:
        embedding_value = vector_literal(embedding) if embedding is not None else None
        return self._fetchone(
            """
            INSERT INTO agent_memory (
              tenant_id, memory_type, content, metadata, source_run_id, embedding
            )
            VALUES (%s, %s, %s, %s::jsonb, %s, %s::vector)
            RETURNING *
            """,
            [tenant_id, memory_type, content, self._json(metadata), source_run_id, embedding_value],
        )

    def search_memory(self, search: MemorySearch) -> list[Mapping[str, Any]]:
        where_sql, where_params = build_memory_where(search)
        where_params = [self._json(param) if isinstance(param, Mapping) else param for param in where_params]
        select_params: list[Any] = []
        order_params: list[Any] = []
        rank_sql = "0::real AS lexical_rank"
        order_parts: list[str] = []
        if search.query:
            rank_sql = "ts_rank_cd(to_tsvector('simple', memory_type || ' ' || content || ' ' || metadata::text), websearch_to_tsquery('simple', %s)) AS lexical_rank"
            select_params.append(search.query)
            order_parts.append("lexical_rank DESC")
        if search.embedding is not None:
            order_params.append(vector_literal(search.embedding))
            order_parts.append("embedding <=> %s::vector")
        order_parts.append("created_at DESC")
        params = [*select_params, *where_params, *order_params, search.limit]
        return self._fetchall(
            f"""
            SELECT id, tenant_id, memory_type, content, metadata, source_run_id, created_at, {rank_sql}
            FROM agent_memory
            WHERE {where_sql}
            ORDER BY {', '.join(order_parts)}
            LIMIT %s
            """,
            params,
        )

    def list_memory_for_run(self, run_id: str, *, limit: int = 100) -> list[Mapping[str, Any]]:
        return self._fetchall(
            """
            SELECT id, tenant_id, memory_type, content, metadata, source_run_id, created_at
            FROM agent_memory
            WHERE source_run_id = %s
            ORDER BY created_at, id
            LIMIT %s
            """,
            [run_id, limit],
        )

    def create_eval_result(
        self,
        tenant_id: str,
        eval_case_id: str,
        run_id: str,
        scores: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return self._fetchone(
            """
            INSERT INTO eval_results (
              tenant_id, eval_case_id, run_id,
              correctness, tool_correctness, retrieval_relevance, format_compliance,
              latency_ms, cost_usd, human_agreement, policy_compliance, scores
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tenant_id, eval_case_id, run_id)
            DO UPDATE SET
              correctness = EXCLUDED.correctness,
              tool_correctness = EXCLUDED.tool_correctness,
              retrieval_relevance = EXCLUDED.retrieval_relevance,
              format_compliance = EXCLUDED.format_compliance,
              latency_ms = EXCLUDED.latency_ms,
              cost_usd = EXCLUDED.cost_usd,
              human_agreement = EXCLUDED.human_agreement,
              policy_compliance = EXCLUDED.policy_compliance,
              scores = EXCLUDED.scores
            RETURNING *
            """,
            [
                tenant_id,
                eval_case_id,
                run_id,
                scores.get("correctness"),
                scores.get("tool_correctness"),
                scores.get("retrieval_relevance"),
                scores.get("format_compliance"),
                scores.get("latency_ms"),
                scores.get("cost_usd"),
                scores.get("human_agreement"),
                scores.get("policy_compliance"),
                self._json(scores),
            ],
        )

    def _execute(self, sql: str, params: Sequence[Any]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def _fetchone(self, sql: str, params: Sequence[Any]) -> Mapping[str, Any] | None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return self._coerce_row(cur, row)

    def _fetchall(self, sql: str, params: Sequence[Any]) -> list[Mapping[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [self._coerce_row(cur, row) for row in cur.fetchall()]

    def _coerce_row(self, cur: Any, row: Any) -> Mapping[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, Mapping):
            return row
        names = [getattr(column, "name", column[0]) for column in cur.description]
        return dict(zip(names, row, strict=True))

    def _json_result(self, row: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
        if row is None:
            raise RuntimeError(f"database function did not return {key}")
        value = row[key]
        if isinstance(value, Mapping):
            return value
        if isinstance(value, str):
            loaded = json.loads(value)
            if isinstance(loaded, Mapping):
                return loaded
        raise RuntimeError(f"database function returned non-object {key}: {value!r}")

    def _json(self, value: Any) -> str:
        return json.dumps(redact_secrets(value), sort_keys=True, default=str)


def migration_files(migrations_dir: str | Path) -> list[Path]:
    return sorted(Path(migrations_dir).glob("*.sql"))
