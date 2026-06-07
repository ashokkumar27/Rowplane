#!/usr/bin/env python3
"""Customer support bridge using OpenAI Agents as a Rowplane command proposer."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rowplane import AgentHarness
from rowplane.adapters import OpenAIAgentsCommandClient

STARTER_PATH = ROOT / "examples" / "starters" / "customer_support_agent" / "agent.py"


def load_support_starter() -> Any:
    spec = importlib.util.spec_from_file_location("rowplane_customer_support_starter", STARTER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load customer support starter: {STARTER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-5.4-mini", help="OpenAI Agents SDK model.")
    parser.add_argument("--max-output-tokens", type=int, default=1200, help="OpenAI Agents SDK max output tokens.")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL/ROWPLANE_DATABASE_URL.")
    return parser


def main() -> None:
    starter = load_support_starter()
    starter.load_dotenv()
    args = build_parser().parse_args()
    database_url = args.database_url or os.environ.get("DATABASE_URL") or os.environ.get("ROWPLANE_DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL or ROWPLANE_DATABASE_URL is required")

    model_client = OpenAIAgentsCommandClient(
        model=args.model,
        max_output_tokens=args.max_output_tokens,
        estimated_call_cost_usd=0.01,
        input_cost_per_million=0.25,
        output_cost_per_million=2.0,
    )

    with AgentHarness(database_url, tenant_id=starter.TENANT_ID, model_client=model_client) as harness:
        harness.migrate()
        harness.set_budget(max_model_calls=50, max_tool_executions=25, max_estimated_cost_usd=5, max_active_work=4)
        for handler in (
            starter.lookup_customer_context,
            starter.search_support_policy,
            starter.issue_refund,
            starter.create_support_ticket,
            starter.update_support_case,
        ):
            harness.register_tool(handler)

        run = harness.create_run(
            starter.support_task("case_openai_agents_9001"),
            model=args.model,
            max_iterations=16,
            required_capabilities=["support:tier1"],
            priority=50,
        )
        harness.drain_leased_work(
            worker_id="openai-agents-support-worker-1",
            kinds=["run"],
            capabilities=["support:tier1", "billing", "refunds"],
            max_steps=12,
        )
        print("after first worker", run.explain())

        approvals = run.approvals()
        if approvals:
            harness.approve(str(approvals[0]["id"]), resolved_by="support-lead")

        harness.drain_leased_work(
            worker_id="openai-agents-support-worker-2",
            kinds=["run"],
            capabilities=["support:tier1", "billing", "refunds"],
            max_steps=24,
        )
        print("final", run.explain())
        print("tool executions", [item["tool_name"] for item in run.tool_executions()])


if __name__ == "__main__":
    main()
