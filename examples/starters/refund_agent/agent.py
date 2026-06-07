#!/usr/bin/env python3
"""Minimal approval-gated refund agent using the developer facade."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pydantic import BaseModel

from rowplane import AgentHarness, tool
from rowplane.samples.use_cases import ScriptedModel

TENANT_ID = "00000000-0000-0000-0000-000000000777"


class RefundInput(BaseModel):
    customer_id: str
    amount_cents: int
    reason: str


@tool(
    input_schema=RefundInput,
    is_side_effecting=True,
    requires_approval=True,
    description="Issue a customer refund after approval.",
)
def issue_refund(ctx, args):
    return {
        "refund_id": f"refund_{ctx.idempotency_key[:10]}",
        "customer_id": args["customer_id"],
        "amount_cents": args["amount_cents"],
        "status": "issued",
    }


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")

    model = ScriptedModel([
        {
            "action": "tool",
            "tool_name": "issue_refund",
            "arguments": {
                "customer_id": "cust_123",
                "amount_cents": 2500,
                "reason": "duplicate charge",
            },
        },
        {
            "action": "tool",
            "tool_name": "issue_refund",
            "arguments": {
                "customer_id": "cust_123",
                "amount_cents": 2500,
                "reason": "duplicate charge",
            },
        },
        {
            "action": "final",
            "answer": {"status": "refund_issued", "customer_id": "cust_123"},
        },
    ])

    with AgentHarness(database_url, tenant_id=TENANT_ID, model_client=model) as harness:
        harness.migrate()
        harness.register_tool(issue_refund)
        run = harness.run({"request": "Refund duplicate charge"})
        print("after first drain", run.explain())

        approvals = run.approvals()
        if approvals:
            harness.approve(str(approvals[0]["id"]), resolved_by="starter-human")
            harness.drain_run(run.run_id)

        print("final", run.explain())


if __name__ == "__main__":
    main()
