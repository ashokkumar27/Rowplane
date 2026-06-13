# Tutorial: Build And Understand A Postgres-Native Agent Harness

This tutorial teaches Rowplane from first principles and then builds real examples.

The core idea is simple:

```text
Postgres is the control plane.
Workers execute I/O.
The model proposes one structured command.
Postgres validates, governs, records, queues, replays, searches, and audits.
```

This is not a LangChain-style orchestration framework. It is a SQL-native runtime ledger for governed agent execution.

## What You Will Learn

You will build and inspect:

1. A tenant-wide budget with one beginner-friendly API.
2. A governed refund agent with an approval-gated tool.
3. Model-call reservation, completion, token, latency, and cost evidence.
4. Tool permissions, input/output schemas, idempotency, and approvals.
5. SQL replay/debugging from `agent_events` and `app.run_trajectory_v`.
6. CLI inspection of the same Postgres rows.
7. Leased worker execution for horizontally scaled workers.
8. The shape of multi-agent delegation and task dependencies.

The examples use a deterministic `ScriptedModel` so behavior is repeatable. Replacing it with a real model adapter does not change the governance path.

## Using A Live OpenAI Model

`ScriptedModel` is useful for demos and tests because it returns known commands. In production, pass a worker-compatible adapter into the same `AgentHarness` constructor:

```python
from rowplane import AgentHarness
from rowplane.adapters import OpenAIModelClient

model = OpenAIModelClient(
    model="gpt-5",
    max_output_tokens=512,
    estimated_call_cost_usd=0.01,
    input_cost_per_million=2.0,
    output_cost_per_million=8.0,
)

with AgentHarness(DATABASE_URL, tenant_id=TENANT_ID, model_client=model) as harness:
    harness.set_budget(max_model_calls=100, max_tool_executions=50, max_estimated_cost_usd=10)
    run = harness.run({"question": "Which policy applies?"})
```

The adapter calls the OpenAI Responses API and returns only the model text. The worker still parses exactly one command, Postgres still validates it, and tools still run only after database permission, schema, approval, and idempotency checks. Token usage is copied into `model_call_completed`; cost is estimated only when you provide pricing rates or a projected per-call cost.

## Mental Model

A run is a state machine over SQL rows.

```text
agent_runs        = current run state
agent_events      = append-only truth
agent_tools       = allowed tool catalog
agent_tool_permissions = who can use each tool
tool_executions   = idempotency and tool results
approval_requests = human gates
agent_memory      = searchable memory
agent_runtime_budgets = global and advanced runtime caps
PGMQ              = wake-up queue
worker            = deterministic interpreter
model             = next-action proposer
```

For multi-agent work, the same idea extends with:

```text
agents                  = specialist definitions
agent_tasks             = child work state
agent_messages          = delegation and result handoffs
agent_task_dependencies = parent/child fan-in coordination
```

The model never directly calls tools, SQL, shell commands, or external APIs. It returns exactly one command, for example:

```json
{"action":"tool","tool_name":"issue_refund","arguments":{"amount_cents":2500}}
```

The worker parses that command. Postgres decides whether it is valid, allowed, approval-gated, idempotent, or blocked.

## Setup

Start Postgres:

```bash
docker compose up -d postgres
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane
```

Install the package locally:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Apply migrations:

```bash
.venv/bin/rowplane --database-url "$DATABASE_URL" migrate
```

Install the optional OpenAI adapter only when you need a live model client:

```bash
.venv/bin/python -m pip install -e '.[openai]'
export OPENAI_API_KEY=...
```

If your local Docker Postgres rejects the password, reset it:

```bash
docker compose exec -T postgres psql -U postgres -d rowplane \
  -c "ALTER USER postgres PASSWORD 'postgres';"
```

## Example 1: A Governed Refund Agent

Create `refund_tutorial.py` at the project root:

```python
from __future__ import annotations

import os

from pydantic import BaseModel

from rowplane import AgentHarness, tool
from rowplane.samples.use_cases import ScriptedModel

TENANT_ID = "00000000-0000-0000-0000-000000000777"


class RefundInput(BaseModel):
    customer_id: str
    amount_cents: int
    reason: str


class RefundOutput(BaseModel):
    refund_id: str
    customer_id: str
    amount_cents: int
    status: str


@tool(
    input_schema=RefundInput,
    output_schema=RefundOutput,
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
        "answer": {
            "status": "refund_issued",
            "customer_id": "cust_123",
            "evidence_tools": ["issue_refund"],
        },
    },
])


def main() -> None:
    database_url = os.environ["DATABASE_URL"]

    with AgentHarness(database_url, tenant_id=TENANT_ID, model_client=model) as harness:
        harness.migrate()

        # One beginner-friendly global budget for the tenant.
        harness.set_budget(
            max_model_calls=20,
            max_tool_executions=10,
            max_estimated_cost_usd=5,
            max_active_work=4,
        )

        # Register the Python handler and persist the tool contract in Postgres.
        harness.register_tool(issue_refund)

        run = harness.create_run(
            {"request": "Refund duplicate charge for cust_123"},
            answer_contract={
                "schema": {
                    "type": "object",
                    "required": ["status", "customer_id", "evidence_tools"],
                    "properties": {
                        "status": {"type": "string"},
                        "customer_id": {"type": "string"},
                        "evidence_tools": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "required_tools": ["issue_refund"],
                "must_reference_tools": True,
            },
        )

        first_outcomes = harness.drain_run(run.run_id)
        print("first outcomes:", first_outcomes)
        print("after first drain:", run.explain())

        approval_id = run.approvals()[0]["id"]
        harness.approve(str(approval_id), resolved_by="human_1")

        second_outcomes = harness.drain_run(run.run_id)
        print("second outcomes:", second_outcomes)
        print("final explanation:", run.explain())
        print("run_id:", run.run_id)


if __name__ == "__main__":
    main()
```

Run it:

```bash
DATABASE_URL="$DATABASE_URL" .venv/bin/python -B refund_tutorial.py
```

Expected behavior:

```text
first drain  -> model asks for issue_refund -> Postgres creates approval -> run waits
approve      -> run requeues
second drain -> tool executes once -> duplicate tool command replays idempotently -> final answer
```

## What Happened Internally

The worker loop did this:

```text
1. Read wake-up message from PGMQ.
2. Load agent_runs row.
3. Move queued -> thinking.
4. Reserve model call through Postgres budget policy.
5. Build prompt from SQL state and event history.
6. Call model.
7. Record model_call_completed or model_call_failed.
8. Parse exactly one command.
9. Submit tool command to Postgres.
10. Postgres validates tool schema and permission.
11. Postgres creates approval request because the tool is risky.
12. After approval, Postgres reserves idempotent tool execution.
13. Worker executes Python handler.
14. Postgres records tool_completed and requeues.
15. Model returns final answer.
16. Postgres validates final-answer contract and completes the run.
```

The important part: every meaningful step became a row.

## Inspect The Control Plane With SQL

Open psql:

```bash
docker compose exec postgres psql -U postgres -d rowplane
```

Set tenant context:

```sql
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-000000000777', false);
```

Inspect the current run state:

```sql
SELECT id, status, iteration_count, max_iterations, task, answer, error
FROM agent_runs
ORDER BY created_at DESC
LIMIT 1;
```

Inspect the event trace:

```sql
SELECT event_id, event_type, actor, payload
FROM agent_events
WHERE run_id = '<run_id>'
ORDER BY event_id;
```

Look for events like:

```text
run_status_changed
run_thinking
runtime_budget_checked
model_call_reserved
model_call_completed
llm_command_received
approval_requested
tool_started
tool_completed
run_completed
```

Inspect the budget row:

```sql
SELECT scope_type, scope_id, max_model_calls, max_tool_executions,
       max_estimated_cost_usd, max_active_work, enabled
FROM agent_runtime_budgets;
```

Inspect model-call accounting:

```sql
SELECT event_type, payload->>'model' AS model,
       payload->>'latency_ms' AS latency_ms,
       payload->>'prompt_tokens' AS prompt_tokens,
       payload->>'completion_tokens' AS completion_tokens,
       payload->>'estimated_cost_usd' AS estimated_cost_usd
FROM agent_events
WHERE run_id = '<run_id>'
  AND event_type IN ('model_call_reserved', 'model_call_completed', 'model_call_failed')
ORDER BY event_id;
```

Inspect the approval:

```sql
SELECT status, reason, payload, resolved_by
FROM approval_requests
WHERE run_id = '<run_id>';
```

Inspect the idempotent tool execution:

```sql
SELECT status, idempotency_key, arguments, result, error
FROM tool_executions
WHERE run_id = '<run_id>';
```

Replay the full trajectory:

```sql
SELECT source, sequence_id, step_type, actor, payload
FROM app.run_trajectory_v
WHERE run_id = '<run_id>'
ORDER BY sequence_id;
```

## Inspect With The CLI

The CLI reads the same rows:

```bash
export TENANT_ID=00000000-0000-0000-0000-000000000777
export RUN_ID=<run_id>

.venv/bin/rowplane --database-url "$DATABASE_URL" explain \
  --tenant-id "$TENANT_ID" "$RUN_ID"

.venv/bin/rowplane --database-url "$DATABASE_URL" events \
  --tenant-id "$TENANT_ID" "$RUN_ID"

.venv/bin/rowplane --database-url "$DATABASE_URL" trajectory \
  --tenant-id "$TENANT_ID" "$RUN_ID"
```

Set or update the tenant budget from CLI:

```bash
.venv/bin/rowplane --database-url "$DATABASE_URL" set-budget \
  --tenant-id "$TENANT_ID" \
  --max-model-calls 1000 \
  --max-tool-executions 500 \
  --max-active-work 10 \
  --max-estimated-cost-usd 25
```

## Example 2: Budget Denial Before The Model Call

Budgets are enforced before external LLM access. Change the tutorial budget to:

```python
harness.set_budget(max_model_calls=0)
```

Run again. The run should block before the model receives messages. In SQL you should see:

```text
runtime_budget_exceeded
model_call_denied_by_budget
run_blocked
```

This matters because the database governs spend and runtime behavior before workers perform external I/O.

## Example 3: Model Spend Budget

A real model adapter can expose projected and actual usage metadata. The worker reads common fields such as:

```python
model.estimated_call_cost_usd = 0.02
model.last_usage = {
    "prompt_tokens": 1200,
    "completion_tokens": 300,
    "total_tokens": 1500,
    "estimated_cost_usd": 0.02,
}
```

`reserve_model_call` checks projected spend before the call. `complete_model_call` records actual usage after the call.

The budget usage is derived from events:

```sql
SELECT app.runtime_cost_budget_scope_usage(
  '00000000-0000-0000-0000-000000000777'::uuid,
  'tenant',
  '00000000-0000-0000-0000-000000000777'
);
```

## Example 4: Leased Workers For Parallel Processing

For simple local demos, `harness.drain_run(...)` is enough. For production-like workers, use SQL leases:

```python
outcomes = harness.drain_leased_work(
    worker_id="worker-1",
    kinds=["run"],
    capabilities=["llm:gpt-5", "tool:refund"],
    max_steps=10,
)
```

Under the hood, Postgres calls:

```sql
SELECT *
FROM app.claim_agent_work(
  '<tenant_id>'::uuid,
  'worker-1',
  ARRAY['llm:gpt-5','tool:refund']::text[],
  10,
  60,
  ARRAY['run']::text[],
  'scheduler'
);
```

Claims create `agent_work_leases` rows and `work_claimed` events. Workers heartbeat and complete leases. If a worker dies, expired leases can be reclaimed.

This is where Postgres plus PgBouncer can scale horizontally: workers are stateless interpreters, while Postgres owns the queue, leases, state, budgets, and trace.

## Example 5: Multi-Agent Delegation

A model can return:

```json
{
  "action": "delegate",
  "to_agent": "policy_researcher",
  "task": {"question":"Find refund policy evidence."},
  "reason": "Need specialist evidence before refund decision."
}
```

Postgres-native multi-agent flow:

```text
planner task -> delegate command
Postgres creates child agent_tasks row
Postgres creates agent_messages delegation row
Postgres creates agent_task_dependencies row
child completes -> task_result message
SQL dependency function releases or blocks parent
parent resumes when required children are satisfied
```

Study the full example:

```bash
sed -n '1,240p' examples/use_cases/multi_agent_refund_review.py
```

Run the real Postgres showcase:

```bash
docker compose up --build postgres-use-cases
```

## How To Add A Real Model

Use the built-in OpenAI adapter when you want the worker to call a live model:

```python
from rowplane.adapters import OpenAIModelClient

model = OpenAIModelClient(
    model="gpt-5",
    max_output_tokens=512,
    estimated_call_cost_usd=0.01,
    input_cost_per_million=2.0,
    output_cost_per_million=8.0,
)
```

A custom model client still only needs a `complete(messages)` method, plus optional `estimated_call_cost_usd` and `last_usage` fields for budget and token accounting. The returned text must parse to exactly one command. The worker rejects malformed commands and writes `llm_command_rejected`.

Workers include registered tool contracts in the prompt state as `registered_tools`. That helps a live model produce exact tool arguments, but it does not weaken governance: invalid tool commands are rejected by Postgres, logged, and requeued for correction instead of failing the run immediately.

Do not put tool execution in the model client. The model proposes. The worker and Postgres govern.

If your app already uses the OpenAI Agents SDK, use the command bridge instead of exposing Rowplane side effects as framework tools:

```python
from rowplane.adapters import OpenAIAgentsCommandClient

model = OpenAIAgentsCommandClient(model="gpt-5.4-mini")
```

That lets the Agent choose the next intended action while Rowplane still owns validation, approval, idempotency, event writes, and tool execution.

If your app uses LangGraph or Deep Agents, use the intent bridges as planner-only adapters:

```python
from rowplane.adapters import DeepAgentsIntentClient, LangGraphIntentClient

model = LangGraphIntentClient(graph=compiled_graph)
# or
model = DeepAgentsIntentClient(agent=compiled_agent)
```

These wrappers emit `RowplaneIntent` objects. They do not expose Rowplane tools to framework-native execution, and they do not decide that approval is required. Rowplane validates the intent, records a policy decision, and maps allowed or approval-gated intents into the existing internal command path.

## Common Patterns

Use `harness.set_budget(...)` first:

```python
harness.set_budget(
    max_model_calls=1000,
    max_tool_executions=500,
    max_estimated_cost_usd=25,
    max_active_work=10,
)
```

Register tools with schemas:

```python
@tool(
    input_schema={"type": "object", "required": ["query"]},
    output_schema={"type": "object", "required": ["documents"]},
)
def search_policy_documents(ctx, args):
    return {"documents": []}
```

Use approval policies for risky tools:

```python
@tool(
    input_schema=RefundInput,
    is_side_effecting=True,
    approval_policy={
        "rules": [{"field": "amount_cents", "operator": "gte", "value": 10000}]
    },
)
def issue_refund(ctx, args):
    return {"status": "issued"}
```

Use final-answer contracts when output shape matters:

```python
run = harness.create_run(
    {"question": "Should we refund?"},
    answer_contract={
        "schema": {"type": "object", "required": ["decision", "evidence_tools"]},
        "required_tools": ["search_policy_documents"],
        "must_reference_tools": True,
    },
)
```

Use replay for debugging:

```python
snapshot = harness.replay(run.run_id)
for step in snapshot["timeline"]:
    print(step["step_type"], step["payload"])
```

Use memory with SQL filters:

```python
rows = harness.search_memory(
    query="refund approval",
    memory_type="case_learning",
    metadata_contains={"domain": "refunds"},
)
```

## Production Checklist

Before running real side effects:

1. Set a tenant budget with `harness.set_budget(...)`.
2. Register every tool in `agent_tools`.
3. Grant only required permissions.
4. Put JSON schemas on tool inputs and outputs.
5. Approval-gate risky tools.
6. Use idempotent handlers for side effects.
7. Use final-answer contracts for critical outputs.
8. Run workers through SQL leases for horizontal scaling.
9. Inspect `agent_events`, `tool_executions`, `approval_requests`, and replay views.
10. Keep secrets out of tool arguments, results, events, and fixtures.

## Where To Go Next

Run these examples:

```bash
.venv/bin/python -B examples/quickstart_sample.py
DATABASE_URL="$DATABASE_URL" .venv/bin/python -B examples/postgres_showcase.py --reset
```

Study:

- `examples/use_cases/refund_approval.py`: approval-gated side effect.
- `examples/use_cases/policy_retrieval_qa.py`: retrieval-style tool use.
- `examples/use_cases/sql_schema_guardrail.py`: SQL-enforced schemas.
- `examples/use_cases/final_answer_contract.py`: final-answer validation.
- `examples/use_cases/multi_agent_refund_review.py`: Postgres-native delegation.
- `examples/use_cases/customer_support_resolution.py`: realistic support workflow with leased workers, approval, memory, and evals.
- `examples/use_cases/trajectory_replay_debug.py`: replay/debug flow.
- `docs/REFERENCE.md`: compact API, CLI, SQL runtime, management API, and examples reference.

## Summary

Rowplane gives you a narrow but powerful contract:

```text
Build agents however you want.
Let Postgres govern their execution.
```

The beginner path is simple: `AgentHarness`, `@tool`, `set_budget`, `run`, `approve`, `replay`.

The advanced path is SQL-native: inspect and govern `agent_runs`, `agent_events`, `tool_executions`, `approval_requests`, budgets, leases, memory, evals, and trajectory views directly in Postgres.
