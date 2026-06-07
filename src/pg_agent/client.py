"""Developer-facing facade over the Postgres-native harness.

This module intentionally keeps Postgres as the control plane. The facade only
packages common setup and inspection workflows so developers do not have to start
by hand-writing every SQL statement.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pg_agent.approvals.service import ApprovalService
from pg_agent.db.migrations import apply_migrations
from pg_agent.db.repository import PostgresRepository
from pg_agent.memory.repository import MemorySearch
from pg_agent.runtime.states import TERMINAL_STATUSES
from pg_agent.tools.base import ToolDefinition, ToolHandler
from pg_agent.tools.executor import ToolExecutor
from pg_agent.tools.registry import ToolRegistry
from pg_agent.workers.lease_worker import AgentLeaseWorker
from pg_agent.workers.worker import AgentWorker, ModelClient


def tool(
    func: ToolHandler | None = None,
    *,
    name: str | None = None,
    input_schema: Mapping[str, Any] | type[Any] | None = None,
    output_schema: Mapping[str, Any] | type[Any] | None = None,
    is_side_effecting: bool = False,
    requires_approval: bool = False,
    approval_policy: Mapping[str, Any] | None = None,
    description: str = "",
) -> Callable[[ToolHandler], ToolHandler] | ToolHandler:
    """Attach a Rowplane tool definition to a Python function.

    ``input_schema`` may be a JSON-schema mapping or a Pydantic model class.
    The worker still validates through ``ToolDefinition`` and Postgres still
    stores the schema in ``agent_tools`` when registered with ``AgentHarness``.
    """

    def decorate(handler: ToolHandler) -> ToolHandler:
        definition = ToolDefinition(
            name=name or handler.__name__,
            handler=handler,
            input_schema=schema_from(input_schema),
            output_schema=schema_from(output_schema),
            is_side_effecting=is_side_effecting,
            requires_approval=requires_approval,
            approval_policy=dict(approval_policy or {}),
            description=description or (getattr(handler, "__doc__", "") or "").strip(),
        )
        setattr(handler, "__pg_agent_tool__", definition)
        return handler

    if func is not None:
        return decorate(func)
    return decorate


def schema_from(schema_or_model: Mapping[str, Any] | type[Any] | None) -> dict[str, Any]:
    if schema_or_model is None:
        return {"type": "object"}
    if isinstance(schema_or_model, Mapping):
        return dict(schema_or_model)
    model_schema = getattr(schema_or_model, "model_json_schema", None)
    if callable(model_schema):
        schema = model_schema()
        if not isinstance(schema, Mapping):
            raise TypeError("Pydantic model_json_schema() must return a mapping")
        return dict(schema)
    raise TypeError("input_schema must be a JSON-schema mapping or Pydantic model class")


def as_tool_definition(candidate: ToolDefinition | ToolHandler) -> ToolDefinition:
    if isinstance(candidate, ToolDefinition):
        return candidate
    definition = getattr(candidate, "__pg_agent_tool__", None)
    if isinstance(definition, ToolDefinition):
        return definition
    raise TypeError("expected ToolDefinition or function decorated with @rowplane.tool")


@dataclass(frozen=True)
class RunHandle:
    """Small handle for inspecting a durable ``agent_runs`` row."""

    harness: AgentHarness
    run_id: str

    @property
    def row(self) -> Mapping[str, Any]:
        run = self.harness.load_run(self.run_id)
        if run is None:
            raise RuntimeError(f"run not found: {self.run_id}")
        return run

    @property
    def status(self) -> str:
        return str(self.row["status"])

    @property
    def answer(self) -> Mapping[str, Any] | None:
        answer = self.row.get("answer")
        return answer if isinstance(answer, Mapping) else None

    def events(self, *, limit: int = 200) -> list[Mapping[str, Any]]:
        return self.harness.events(self.run_id, limit=limit)

    def trajectory(self, *, limit: int = 500) -> list[Mapping[str, Any]]:
        return self.harness.trajectory(self.run_id, limit=limit)

    def approvals(self) -> list[Mapping[str, Any]]:
        return self.harness.approvals(self.run_id)

    def tool_executions(self) -> list[Mapping[str, Any]]:
        return self.harness.tool_executions(self.run_id)

    def explain(self) -> Mapping[str, Any]:
        return self.harness.explain(self.run_id)


class AgentHarness:
    """Low-friction API that still uses Postgres as the source of truth."""

    def __init__(
        self,
        database_url: str,
        *,
        tenant_id: str | None = None,
        model_client: ModelClient | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency is part of package install
            raise RuntimeError("psycopg is required to use AgentHarness") from exc

        self.database_url = database_url
        self.tenant_id = tenant_id
        self.conn = psycopg.connect(database_url, row_factory=dict_row, autocommit=False)
        self.repo = PostgresRepository(self.conn)
        self.registry = registry or ToolRegistry()
        self.model_client = model_client
        self._owns_connection = True
        if tenant_id is not None:
            self.repo.set_tenant(tenant_id)

    @classmethod
    def from_connection(
        cls,
        conn: Any,
        *,
        tenant_id: str | None = None,
        model_client: ModelClient | None = None,
        registry: ToolRegistry | None = None,
    ) -> AgentHarness:
        harness = cls.__new__(cls)
        harness.database_url = "<existing-connection>"
        harness.tenant_id = tenant_id
        harness.conn = conn
        harness.repo = PostgresRepository(conn)
        harness.registry = registry or ToolRegistry()
        harness.model_client = model_client
        harness._owns_connection = False
        if tenant_id is not None:
            harness.repo.set_tenant(tenant_id)
        return harness

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    def __enter__(self) -> AgentHarness:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owns_connection:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        self.close()

    def migrate(self) -> list[str]:
        applied = apply_migrations(self.conn)
        self.conn.commit()
        return applied

    def set_tenant(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.repo.set_tenant(tenant_id)

    def register_tool(
        self,
        definition_or_handler: ToolDefinition | ToolHandler,
        *,
        grant_to_tenant: bool = True,
        enabled: bool = True,
    ) -> Mapping[str, Any]:
        self._require_tenant()
        definition = as_tool_definition(definition_or_handler)
        row = self.repo.upsert_tool(
            self.tenant_id,
            definition.name,
            description=definition.description,
            input_schema=definition.input_schema,
            output_schema=definition.output_schema,
            is_side_effecting=definition.is_side_effecting,
            requires_approval=definition.requires_approval,
            approval_policy=definition.approval_policy,
            enabled=enabled,
        )
        self.registry.register(definition)
        if grant_to_tenant:
            self.repo.grant_tool_permission(
                self.tenant_id,
                str(row["id"]),
                "tenant",
                self.tenant_id,
                allowed=True,
            )
        self.conn.commit()
        return row

    def register_tool_catalog(
        self,
        name: str,
        *,
        input_schema: Mapping[str, Any] | type[Any] | None = None,
        description: str = "",
        is_side_effecting: bool = False,
        requires_approval: bool = False,
        output_schema: Mapping[str, Any] | type[Any] | None = None,
        approval_policy: Mapping[str, Any] | None = None,
        grant_to_tenant: bool = True,
        enabled: bool = True,
    ) -> Mapping[str, Any]:
        self._require_tenant()
        row = self.repo.upsert_tool(
            self.tenant_id,
            name,
            description=description,
            input_schema=schema_from(input_schema),
            output_schema=schema_from(output_schema),
            is_side_effecting=is_side_effecting,
            requires_approval=requires_approval,
            approval_policy=dict(approval_policy or {}),
            enabled=enabled,
        )
        if grant_to_tenant:
            self.repo.grant_tool_permission(self.tenant_id, str(row["id"]), "tenant", self.tenant_id)
        self.conn.commit()
        return row

    def grant_tool(
        self,
        tool_name: str,
        *,
        subject_type: str = "tenant",
        subject_id: str | None = None,
        allowed: bool = True,
    ) -> Mapping[str, Any]:
        self._require_tenant()
        tool_row = self.repo.get_agent_tool(self.tenant_id, tool_name)
        if tool_row is None:
            raise RuntimeError(f"tool not found: {tool_name}")
        return self.repo.grant_tool_permission(
            self.tenant_id,
            str(tool_row["id"]),
            subject_type,
            subject_id or self.tenant_id,
            allowed=allowed,
        )

    def register_agent(
        self,
        name: str,
        *,
        role: str,
        instructions: str,
        model: str = "unset",
        enabled: bool = True,
    ) -> Mapping[str, Any]:
        self._require_tenant()
        row = self.repo.upsert_agent(
            self.tenant_id,
            name,
            role=role,
            instructions=instructions,
            model=model,
            enabled=enabled,
        )
        self.conn.commit()
        return row

    def set_budget(
        self,
        *,
        max_model_calls: int | None = None,
        max_tool_executions: int | None = None,
        max_child_tasks: int | None = None,
        max_active_work: int | None = None,
        max_estimated_cost_usd: float | None = None,
        enabled: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Set the simple tenant-wide runtime budget.

        This is the beginner-facing budget API. It writes one
        `agent_runtime_budgets` row scoped to the current tenant; advanced
        run/task/agent-scoped rows can still be managed directly in SQL.
        """

        self._require_tenant()
        row = self.repo.upsert_tenant_budget(
            self.tenant_id,
            max_model_calls=max_model_calls,
            max_tool_executions=max_tool_executions,
            max_child_tasks=max_child_tasks,
            max_active_work=max_active_work,
            max_estimated_cost_usd=max_estimated_cost_usd,
            enabled=enabled,
            metadata=metadata,
        )
        self.conn.commit()
        return row

    def get_budget(self) -> Mapping[str, Any] | None:
        self._require_tenant()
        return self.repo.get_tenant_budget(self.tenant_id)

    def create_run(
        self,
        task: Mapping[str, Any],
        *,
        run_id: str | None = None,
        model: str = "sample-scripted-model",
        max_iterations: int = 8,
        answer_contract: Mapping[str, Any] | None = None,
        queue: bool = True,
        required_capabilities: Sequence[str] | None = None,
        priority: int = 0,
        not_before: Any = None,
        deadline_at: Any = None,
    ) -> RunHandle:
        self._require_tenant()
        resolved_run_id = run_id or str(uuid4())
        run_task = dict(task)
        if answer_contract is not None:
            run_task["answer_contract"] = dict(answer_contract)
        self.repo.create_run(
            self.tenant_id,
            resolved_run_id,
            run_task,
            model=model,
            max_iterations=max_iterations,
            required_capabilities=required_capabilities,
            priority=priority,
            not_before=not_before,
            deadline_at=deadline_at,
        )
        if queue:
            self.repo.queue_run(self.tenant_id, resolved_run_id)
        self.conn.commit()
        return RunHandle(self, resolved_run_id)

    def run(
        self,
        task: Mapping[str, Any],
        *,
        drain: bool = True,
        max_steps: int = 20,
        **create_kwargs: Any,
    ) -> RunHandle:
        handle = self.create_run(task, **create_kwargs)
        if drain:
            self.drain_run(handle.run_id, max_steps=max_steps)
        return handle

    def drain_run(self, run_id: str, *, max_steps: int = 20) -> list[str]:
        if self.model_client is None:
            raise RuntimeError("drain_run requires a model_client")
        worker = AgentWorker(self.repo, self.model_client, ToolExecutor(self.repo, self.registry))
        outcomes: list[str] = []
        terminal = {str(status) for status in TERMINAL_STATUSES}
        for _ in range(max_steps):
            outcome = worker.run_once()
            self.conn.commit()
            outcomes.append(outcome)
            run = self.repo.load_run(run_id)
            if run is None or outcome == "empty" or str(run["status"]) in terminal | {"waiting_approval"}:
                break
        return outcomes

    def run_until_terminal(
        self,
        run_id: str,
        *,
        max_steps: int = 30,
        max_approval_cycles: int = 5,
        approval_handler: Callable[[Mapping[str, Any]], bool] | None = None,
        resolved_by: str = "harness",
    ) -> list[str]:
        """Drain a run through approval cycles until it reaches a terminal state.

        ``drain_run`` intentionally stops at ``waiting_approval`` so callers can
        inspect the control-plane row. This higher-level helper is explicit
        about approval policy: without ``approval_handler`` it also stops at the
        pending approval; with a handler it resolves each pending approval and
        resumes the run until terminal, empty queue, or guard limit.
        """

        outcomes: list[str] = []
        terminal = {str(status) for status in TERMINAL_STATUSES}
        approval_cycles = 0
        remaining_steps = max_steps

        while remaining_steps > 0:
            before = len(outcomes)
            batch = self.drain_run(run_id, max_steps=remaining_steps)
            outcomes.extend(batch)
            remaining_steps -= max(len(outcomes) - before, 1)

            run = self.repo.load_run(run_id)
            if run is None or str(run["status"]) in terminal:
                break
            if str(run["status"]) != "waiting_approval":
                if not batch or batch[-1] == "empty":
                    break
                continue
            if approval_handler is None:
                break
            if approval_cycles >= max_approval_cycles:
                break

            pending = [approval for approval in self.repo.list_approval_requests(run_id) if approval.get("status") == "pending"]
            if not pending:
                break
            approval_cycles += 1
            for approval in pending:
                decision = bool(approval_handler(approval))
                if decision:
                    self.approve(str(approval["id"]), resolved_by=resolved_by)
                else:
                    self.reject(str(approval["id"]), resolved_by=resolved_by)
                    break

        return outcomes

    def drain_leased_work(
        self,
        *,
        worker_id: str = "harness",
        max_steps: int = 20,
        lease_seconds: int = 60,
        capabilities: Sequence[str] | None = None,
        kinds: Sequence[str] | None = None,
    ) -> list[str]:
        if self.model_client is None:
            raise RuntimeError("drain_leased_work requires a model_client")
        self._require_tenant()
        worker = AgentLeaseWorker(
            self.repo,
            self.model_client,
            self.registry,
            worker_id=worker_id,
            tenant_id=self.tenant_id,
            capabilities=capabilities,
            lease_seconds=lease_seconds,
            kinds=kinds,
        )
        outcomes: list[str] = []
        for _ in range(max_steps):
            outcome = worker.run_once()
            self.conn.commit()
            outcomes.append(outcome)
            if outcome == "empty":
                break
        return outcomes

    def load_run(self, run_id: str) -> Mapping[str, Any] | None:
        return self.repo.load_run(run_id)

    def events(self, run_id: str, *, limit: int = 200) -> list[Mapping[str, Any]]:
        return self.repo.load_events(run_id, limit=limit)

    def approvals(self, run_id: str) -> list[Mapping[str, Any]]:
        return self.repo.list_approval_requests(run_id)

    def tool_executions(self, run_id: str) -> list[Mapping[str, Any]]:
        return self.repo.list_tool_executions(run_id)

    def trajectory(self, run_id: str, *, limit: int = 500) -> list[Mapping[str, Any]]:
        self._require_tenant()
        return self.repo.list_run_trajectory(self.tenant_id, run_id, limit=limit)

    def replay(self, run_id: str, *, limit: int = 500) -> Mapping[str, Any]:
        run = self.repo.load_run(run_id)
        if run is None:
            raise RuntimeError(f"run not found: {run_id}")
        return {
            "run": run,
            "timeline": self.trajectory(run_id, limit=limit),
            "events": self.events(run_id, limit=limit),
            "tool_executions": self.tool_executions(run_id),
            "approvals": self.approvals(run_id),
            "memory": self.repo.list_memory_for_run(run_id) if hasattr(self.repo, "list_memory_for_run") else [],
        }

    def search(self, query: str, *, limit: int = 50) -> list[Mapping[str, Any]]:
        self._require_tenant()
        return self.repo.search_harness(self.tenant_id, query, limit=limit)

    def search_memory(
        self,
        *,
        query: str | None = None,
        memory_type: str | None = None,
        metadata_contains: Mapping[str, Any] | None = None,
        source_run_id: str | None = None,
        embedding: Sequence[float] | None = None,
        limit: int = 10,
        record_event_for_run_id: str | None = None,
    ) -> list[Mapping[str, Any]]:
        self._require_tenant()
        search = MemorySearch(
            tenant_id=self.tenant_id,
            memory_type=memory_type,
            metadata_contains=dict(metadata_contains or {}),
            source_run_id=source_run_id,
            query=query,
            embedding=embedding,
            limit=limit,
        )
        rows = self.repo.search_memory(search)
        if record_event_for_run_id is not None:
            self.repo.append_event(
                self.tenant_id,
                record_event_for_run_id,
                "memory_search_performed",
                {
                    "query": query,
                    "memory_type": memory_type,
                    "metadata_contains": dict(metadata_contains or {}),
                    "source_run_id": source_run_id,
                    "result_ids": [str(row["id"]) for row in rows],
                },
                actor="harness",
            )
            self.conn.commit()
        return rows

    def approve(self, approval_id: str, *, resolved_by: str) -> Mapping[str, Any]:
        resolved = ApprovalService(self.repo).resolve(approval_id, approved=True, resolved_by=resolved_by)
        self.conn.commit()
        return resolved

    def reject(self, approval_id: str, *, resolved_by: str) -> Mapping[str, Any]:
        resolved = ApprovalService(self.repo).resolve(approval_id, approved=False, resolved_by=resolved_by)
        self.conn.commit()
        return resolved

    def explain(self, run_id: str) -> Mapping[str, Any]:
        run = self.repo.load_run(run_id)
        if run is None:
            raise RuntimeError(f"run not found: {run_id}")
        events = self.repo.load_events(run_id, limit=500)
        approvals = self.repo.list_approval_requests(run_id)
        executions = self.repo.list_tool_executions(run_id)
        event_types = [str(event["event_type"]) for event in events]
        requested_tools = [
            event["payload"].get("tool_name")
            for event in events
            if event["event_type"] == "llm_command_received"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("action") == "tool"
        ]
        pending = [approval for approval in approvals if approval.get("status") == "pending"]
        return {
            "run_id": run_id,
            "status": str(run["status"]),
            "error": run.get("error"),
            "answer": run.get("answer"),
            "iteration_count": run.get("iteration_count"),
            "max_iterations": run.get("max_iterations"),
            "last_event": event_types[-1] if event_types else None,
            "requested_tools": [tool for tool in requested_tools if tool],
            "pending_approval_ids": [str(approval["id"]) for approval in pending],
            "permission_denials": [event.get("payload", {}) for event in events if event["event_type"] == "tool_permission_denied"],
            "validation_failures": [event.get("payload", {}) for event in events if event["event_type"] == "tool_validation_failed"],
            "tool_executions": [
                {
                    "id": str(execution["id"]),
                    "status": str(execution["status"]),
                    "error": execution.get("error"),
                }
                for execution in executions
            ],
        }

    def _require_tenant(self) -> None:
        if self.tenant_id is None:
            raise RuntimeError("tenant_id is required for this operation")


def to_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))
