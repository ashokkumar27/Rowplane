"""Command-line entry point for the developer-facing harness API."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from pg_agent.client import AgentHarness, to_jsonable


def build_parser(*, prog: str = "rowplane") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Developer CLI for Rowplane, the Postgres-native agent harness.",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL") or os.environ.get("ROWPLANE_DATABASE_URL") or os.environ.get("PG_AGENT_DATABASE_URL"),
        help="Postgres connection URL. Defaults to DATABASE_URL, ROWPLANE_DATABASE_URL, or legacy PG_AGENT_DATABASE_URL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="Apply database migrations.")
    migrate.set_defaults(func=cmd_migrate)

    register_tool = subparsers.add_parser("register-tool", help="Register or update a tool in agent_tools.")
    add_tenant(register_tool)
    register_tool.add_argument("--name", required=True)
    register_tool.add_argument("--description", default="")
    register_tool.add_argument("--schema-json", default='{"type":"object"}', help="Tool input JSON schema.")
    register_tool.add_argument("--output-schema-json", default='{"type":"object"}', help="Tool output JSON schema.")
    register_tool.add_argument("--approval-policy-json", default="{}", help="Declarative approval policy JSON.")
    register_tool.add_argument("--side-effecting", action="store_true")
    register_tool.add_argument("--requires-approval", action="store_true")
    register_tool.add_argument("--no-grant-tenant", action="store_true")
    register_tool.set_defaults(func=cmd_register_tool)

    register_agent = subparsers.add_parser("register-agent", help="Register or update an agent row.")
    add_tenant(register_agent)
    register_agent.add_argument("--name", required=True)
    register_agent.add_argument("--role", required=True)
    register_agent.add_argument("--instructions", required=True)
    register_agent.add_argument("--model", default="unset")
    register_agent.set_defaults(func=cmd_register_agent)

    set_budget = subparsers.add_parser("set-budget", help="Set the tenant-wide runtime budget.")
    add_tenant(set_budget)
    set_budget.add_argument("--max-model-calls", type=int, default=None)
    set_budget.add_argument("--max-tool-executions", type=int, default=None)
    set_budget.add_argument("--max-child-tasks", type=int, default=None)
    set_budget.add_argument("--max-active-work", type=int, default=None)
    set_budget.add_argument("--max-estimated-cost-usd", type=float, default=None)
    set_budget.add_argument("--metadata-json", default="{}")
    set_budget.add_argument("--disabled", action="store_true")
    set_budget.set_defaults(func=cmd_set_budget)

    run = subparsers.add_parser("run", help="Create an agent_runs row and queue it through PGMQ.")
    add_tenant(run)
    run.add_argument("--task-json", required=True)
    run.add_argument("--model", default="sample-scripted-model")
    run.add_argument("--max-iterations", type=int, default=8)
    run.add_argument("--answer-contract-json", default=None)
    run.add_argument("--no-queue", action="store_true")
    run.set_defaults(func=cmd_run)

    events = subparsers.add_parser("events", help="List run events.")
    add_tenant(events)
    events.add_argument("run_id")
    events.add_argument("--limit", type=int, default=200)
    events.set_defaults(func=cmd_events)

    trajectory = subparsers.add_parser("trajectory", help="Replay a run trajectory from SQL rows.")
    add_tenant(trajectory)
    trajectory.add_argument("run_id")
    trajectory.add_argument("--limit", type=int, default=500)
    trajectory.set_defaults(func=cmd_trajectory)

    replay = subparsers.add_parser("replay", help="Return a complete run replay/debug snapshot.")
    add_tenant(replay)
    replay.add_argument("run_id")
    replay.add_argument("--limit", type=int, default=500)
    replay.set_defaults(func=cmd_replay)

    explain = subparsers.add_parser("explain", help="Explain run state, approvals, tools, and failures.")
    add_tenant(explain)
    explain.add_argument("run_id")
    explain.set_defaults(func=cmd_explain)

    approve = subparsers.add_parser("approve", help="Approve a pending approval request and requeue work.")
    add_tenant(approve)
    approve.add_argument("approval_id")
    approve.add_argument("--by", default="cli")
    approve.set_defaults(func=cmd_approve)

    reject = subparsers.add_parser("reject", help="Reject a pending approval request and block work.")
    add_tenant(reject)
    reject.add_argument("approval_id")
    reject.add_argument("--by", default="cli")
    reject.set_defaults(func=cmd_reject)

    search = subparsers.add_parser("search", help="Search harness events, memory, tools, and evals.")
    add_tenant(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=cmd_search)

    memory_search = subparsers.add_parser("search-memory", help="Search tenant memory with SQL filters.")
    add_tenant(memory_search)
    memory_search.add_argument("--query", default=None)
    memory_search.add_argument("--memory-type", default=None)
    memory_search.add_argument("--metadata-json", default="{}")
    memory_search.add_argument("--source-run-id", default=None)
    memory_search.add_argument("--record-event-for-run-id", default=None)
    memory_search.add_argument("--limit", type=int, default=10)
    memory_search.set_defaults(func=cmd_search_memory)

    return parser


def add_tenant(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant-id", required=True)


def cmd_migrate(args: argparse.Namespace) -> int:
    with harness(args) as app:
        applied = app.migrate()
    print_json({"applied": applied})
    return 0


def cmd_register_tool(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        row = app.register_tool_catalog(
            args.name,
            input_schema=parse_json(args.schema_json),
            output_schema=parse_json(args.output_schema_json),
            approval_policy=parse_json(args.approval_policy_json),
            description=args.description,
            is_side_effecting=args.side_effecting,
            requires_approval=args.requires_approval,
            grant_to_tenant=not args.no_grant_tenant,
        )
    print_json(row)
    return 0


def cmd_register_agent(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        row = app.register_agent(
            args.name,
            role=args.role,
            instructions=args.instructions,
            model=args.model,
        )
    print_json(row)
    return 0


def cmd_set_budget(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        row = app.set_budget(
            max_model_calls=args.max_model_calls,
            max_tool_executions=args.max_tool_executions,
            max_child_tasks=args.max_child_tasks,
            max_active_work=args.max_active_work,
            max_estimated_cost_usd=args.max_estimated_cost_usd,
            metadata=parse_json(args.metadata_json),
            enabled=not args.disabled,
        )
    print_json(row)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        handle = app.create_run(
            parse_json(args.task_json),
            model=args.model,
            max_iterations=args.max_iterations,
            answer_contract=parse_json(args.answer_contract_json) if args.answer_contract_json else None,
            queue=not args.no_queue,
        )
        row = handle.row
    print_json(row)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        rows = app.events(args.run_id, limit=args.limit)
    print_json(rows)
    return 0


def cmd_trajectory(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        rows = app.trajectory(args.run_id, limit=args.limit)
    print_json(rows)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        replay = app.replay(args.run_id, limit=args.limit)
    print_json(replay)
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        explanation = app.explain(args.run_id)
    print_json(explanation)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        row = app.approve(args.approval_id, resolved_by=args.by)
    print_json(row)
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        row = app.reject(args.approval_id, resolved_by=args.by)
    print_json(row)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        rows = app.search(args.query, limit=args.limit)
    print_json(rows)
    return 0


def cmd_search_memory(args: argparse.Namespace) -> int:
    with harness(args, tenant_id=args.tenant_id) as app:
        rows = app.search_memory(
            query=args.query,
            memory_type=args.memory_type,
            metadata_contains=parse_json(args.metadata_json),
            source_run_id=args.source_run_id,
            record_event_for_run_id=args.record_event_for_run_id,
            limit=args.limit,
        )
    print_json(rows)
    return 0


def harness(args: argparse.Namespace, *, tenant_id: str | None = None) -> AgentHarness:
    if not args.database_url:
        raise SystemExit("--database-url, DATABASE_URL, ROWPLANE_DATABASE_URL, or legacy PG_AGENT_DATABASE_URL is required")
    return AgentHarness(args.database_url, tenant_id=tenant_id)


def parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON: {exc}") from exc


def print_json(value: Any) -> None:
    print(json.dumps(to_jsonable(value), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    prog = "rowplane" if argv is not None else (os.path.basename(sys.argv[0]) or "rowplane")
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
