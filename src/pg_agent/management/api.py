"""FastAPI management API for Rowplane."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pg_agent.approvals.service import ApprovalService
from pg_agent.management.repository import ManagementRepository
from pg_agent.runtime.errors import ApprovalAlreadyResolved, ApprovalNotFound


class ApprovalDecision(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ToolUpdate(BaseModel):
    enabled: bool | None = None
    requires_approval: bool | None = None
    description: str | None = Field(default=None, max_length=1000)


TenantHeader = Annotated[str, Header(alias="X-Tenant-ID")]
ActorHeader = Annotated[str, Header(alias="X-Actor")]
LimitQuery = Annotated[int, Query(ge=1, le=500)]
OffsetQuery = Annotated[int, Query(ge=0)]
RepositoryFactory = Callable[[], Any]


def create_app(
    *,
    database_url: str | None = None,
    repository_factory: RepositoryFactory | None = None,
) -> FastAPI:
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(
        title="Rowplane Management API",
        version="0.1.0",
        description="API-first operations and developer console for Rowplane, the Postgres-native agent harness.",
    )
    if static_dir.exists():
        app.mount(
            "/console/assets",
            StaticFiles(directory=static_dir),
            name="console_assets",
        )

    def get_repository() -> Iterator[Any]:
        if repository_factory is not None:
            yield repository_factory()
            return

        url = database_url or os.environ.get("DATABASE_URL")
        if not url:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="DATABASE_URL is required",
            )
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="psycopg is required",
            ) from exc

        conn = psycopg.connect(url, row_factory=dict_row, autocommit=False)
        try:
            repo = ManagementRepository(conn)
            yield repo
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def tenant_repo(
        tenant_id: TenantHeader,
        repo: Any = Depends(get_repository),
    ) -> Any:
        if hasattr(repo, "set_tenant"):
            repo.set_tenant(tenant_id)
        return repo

    @app.get("/health")
    def health() -> Mapping[str, str]:
        return {"status": "ok"}

    @app.get("/console", include_in_schema=False)
    @app.get("/console/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/metrics/overview")
    def metrics_overview(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
    ) -> Mapping[str, Any]:
        return repo.management_overview(tenant_id)

    @app.get("/api/approvals")
    def list_approvals(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
        status_filter: Annotated[str | None, Query(alias="status")] = "pending",
        run_id: str | None = None,
        task_id: str | None = None,
        tool_name: str | None = None,
        limit: LimitQuery = 50,
        offset: OffsetQuery = 0,
    ) -> list[Mapping[str, Any]]:
        status_value = None if status_filter in {None, "all"} else status_filter
        return repo.list_management_approvals(
            tenant_id,
            status=status_value,
            run_id=run_id,
            task_id=task_id,
            tool_name=tool_name,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/approvals/{approval_id}")
    def get_approval(
        approval_id: str,
        repo: Any = Depends(tenant_repo),
    ) -> Mapping[str, Any]:
        approval = repo.get_management_approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        return approval

    @app.post("/api/approvals/{approval_id}/approve")
    def approve(
        approval_id: str,
        repo: Any = Depends(tenant_repo),
        actor: ActorHeader = "management_api",
    ) -> Mapping[str, Any]:
        return _resolve_approval(repo, approval_id, approved=True, actor=actor)

    @app.post("/api/approvals/{approval_id}/reject")
    def reject(
        approval_id: str,
        repo: Any = Depends(tenant_repo),
        decision: ApprovalDecision | None = None,
        actor: ActorHeader = "management_api",
    ) -> Mapping[str, Any]:
        result = _resolve_approval(repo, approval_id, approved=False, actor=actor)
        if decision and decision.reason:
            result = dict(result)
            result["management_reason"] = decision.reason
        return result

    @app.get("/api/runs")
    def list_runs(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
        status_filter: Annotated[str | None, Query(alias="status")] = None,
        agent_id: str | None = None,
        eval_case_id: str | None = None,
        limit: LimitQuery = 50,
        offset: OffsetQuery = 0,
    ) -> list[Mapping[str, Any]]:
        return repo.list_management_runs(
            tenant_id,
            status=status_filter,
            agent_id=agent_id,
            eval_case_id=eval_case_id,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/runs/{run_id}")
    def get_run(
        run_id: str,
        repo: Any = Depends(tenant_repo),
    ) -> Mapping[str, Any]:
        run = repo.get_management_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return run

    @app.get("/api/runs/{run_id}/timeline")
    def get_run_timeline(
        run_id: str,
        repo: Any = Depends(tenant_repo),
        limit: LimitQuery = 300,
    ) -> list[Mapping[str, Any]]:
        return repo.get_management_run_timeline(run_id, limit=limit)

    @app.get("/api/runs/{run_id}/trajectory")
    def get_run_trajectory(
        run_id: str,
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
        limit: LimitQuery = 500,
    ) -> list[Mapping[str, Any]]:
        return repo.get_management_run_trajectory(tenant_id, run_id, limit=limit)

    @app.get("/api/search")
    def search_harness(
        tenant_id: TenantHeader,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        repo: Any = Depends(tenant_repo),
        limit: LimitQuery = 50,
    ) -> list[Mapping[str, Any]]:
        return repo.search_management_harness(tenant_id, q, limit=limit)

    @app.post("/api/runs/{run_id}/retry")
    def retry_run(
        run_id: str,
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
    ) -> Mapping[str, Any]:
        try:
            return repo.retry_management_run(tenant_id, run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/tools")
    def list_tools(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
    ) -> list[Mapping[str, Any]]:
        return repo.list_management_tools(tenant_id)

    @app.patch("/api/tools/{tool_id}")
    def update_tool(
        tool_id: str,
        update: ToolUpdate,
        repo: Any = Depends(tenant_repo),
        actor: ActorHeader = "management_api",
    ) -> Mapping[str, Any]:
        if (
            update.enabled is None
            and update.requires_approval is None
            and update.description is None
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="at least one tool field is required",
            )
        try:
            return repo.update_management_tool(
                tool_id,
                enabled=update.enabled,
                requires_approval=update.requires_approval,
                description=update.description,
                actor=actor,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/agents")
    def list_agents(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
    ) -> list[Mapping[str, Any]]:
        return repo.list_management_agents(tenant_id)

    @app.get("/api/agents/{agent_id}")
    def get_agent(
        agent_id: str,
        repo: Any = Depends(tenant_repo),
    ) -> Mapping[str, Any]:
        agent = repo.get_management_agent(agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="agent not found")
        return agent

    @app.get("/api/evals")
    def list_evals(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
    ) -> list[Mapping[str, Any]]:
        return repo.list_management_eval_summaries(tenant_id)

    @app.get("/api/evals/{eval_case_id}/results")
    def list_eval_results(
        eval_case_id: str,
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
        limit: LimitQuery = 50,
        offset: OffsetQuery = 0,
    ) -> list[Mapping[str, Any]]:
        return repo.list_management_eval_results(tenant_id, eval_case_id, limit=limit, offset=offset)

    @app.get("/api/audit/events")
    def list_audit_events(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
        run_id: str | None = None,
        event_type: str | None = None,
        actor: str | None = None,
        limit: LimitQuery = 100,
        offset: OffsetQuery = 0,
    ) -> list[Mapping[str, Any]]:
        return repo.list_management_audit_events(
            tenant_id,
            run_id=run_id,
            event_type=event_type,
            actor=actor,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/memory")
    def list_memory(
        tenant_id: TenantHeader,
        repo: Any = Depends(tenant_repo),
        memory_type: str | None = None,
        source_run_id: str | None = None,
        limit: LimitQuery = 50,
        offset: OffsetQuery = 0,
    ) -> list[Mapping[str, Any]]:
        return repo.list_management_memory(
            tenant_id,
            memory_type=memory_type,
            source_run_id=source_run_id,
            limit=limit,
            offset=offset,
        )

    return app


def _resolve_approval(
    repo: Any,
    approval_id: str,
    *,
    approved: bool,
    actor: str,
) -> Mapping[str, Any]:
    try:
        return ApprovalService(repo).resolve(
            approval_id,
            approved=approved,
            resolved_by=actor,
        )
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApprovalAlreadyResolved as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


app = create_app()
