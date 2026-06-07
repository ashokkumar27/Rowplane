# Rowplane

![Rowplane Postgres-native agent control plane](docs/assets/rowplane-hero.webp)

Rowplane is a Postgres-native control plane for governed AI agents.

Build agents however you want. Rowplane gives them a durable place to run: every state change, tool call, approval, budget decision, memory write, and final answer is recorded in Postgres.

A useful way to think about it:

```text
Rowplane = job queue + audit log + policy gate for agent runs
```

The model does not directly call tools or APIs. It proposes one structured command. A worker interprets that command. Postgres decides whether it is valid, allowed, approval-gated, budget-safe, idempotent, and ready to continue.

That is the core idea.

## Project Status

Rowplane is early. The core runtime is tested, but large-scale production hardening is still in progress. See [Scaling Notes](docs/SCALING.md) before making production scale claims.

## Why This Exists

Most agent systems are easy to start but hard to operate.

Once agents touch real workflows, teams need answers to boring but important questions:

- What happened in this run?
- Which tool was called, with what input, and by whom?
- Was approval required?
- Did this tenant exceed budget?
- Can we replay or debug the trajectory?
- Can multiple workers process runs without double-executing side effects?

Rowplane keeps those answers in SQL rows instead of hidden process memory.

## What It Is

- A SQL-first runtime ledger for AI agents.
- A small Python facade and CLI over the same Postgres rows.
- A worker pattern where workers execute I/O, but Postgres governs the run.
- A fit for teams that care about auditability, approvals, tenant boundaries, replay, evals, and reliable tool execution.

## What It Is Not

Rowplane is not another agent orchestration framework.

It does not use LangChain, LangGraph, CrewAI, Temporal, Redis, Kafka, or another external orchestration layer. The niche is narrower:

```text
Build agents however you want. Govern their execution in Postgres.
```

## Mental Model

```text
agent_runs      = current state
agent_events    = append-only truth
agent_tools     = allowed tool catalog
tool_executions = idempotency and results
approvals       = human gates
agent_memory    = searchable knowledge
PGMQ            = wake-up queue
workers         = stateless interpreters
model           = next-action proposer
```

For multi-agent work, Rowplane adds `agents`, `agent_tasks`, `agent_messages`, and `agent_task_dependencies`. The same rule still applies: Postgres is the control plane.

## Architecture

```mermaid
flowchart TD
    A[App / CLI / Console] --> P[Postgres Control Plane]

    P --> R[agent_runs<br/>current state]
    P --> E[agent_events<br/>append-only truth]
    P --> T[agent_tools<br/>allowed actions]
    P --> G[approvals + budgets<br/>governance]
    P --> M[agent_memory<br/>searchable knowledge]
    P --> Q[PGMQ<br/>wake-up queue]

    Q --> W[Stateless Workers]
    W --> L[LLMs]
    W --> X[External Tools / APIs]
    W --> P
```

Think of Postgres as the dispatcher and flight recorder. Workers can come and go, but the run state, queue, permissions, approvals, budgets, and trace stay durable in the database.

## Docs

Start here:

1. [Tutorial](docs/TUTORIAL.md): build a governed refund agent end to end.
2. [Reference](docs/REFERENCE.md): API, CLI, SQL runtime, management API, examples, and tests.
3. [Scaling Notes](docs/SCALING.md): current scale posture, bottlenecks, and future production-readiness work.
4. [Examples](examples): executable examples.

## Quick Start

Start Postgres and install locally:

```bash
docker compose up -d postgres
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane
```

Apply migrations:

```bash
.venv/bin/rowplane --database-url "$DATABASE_URL" migrate
```

Save this as `quickstart.py`:

```python
import os

from rowplane import AgentHarness, tool
from rowplane.samples.use_cases import ScriptedModel

TENANT_ID = "00000000-0000-0000-0000-000000000777"


@tool(
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
        "additionalProperties": False,
    },
    description="Search internal policy notes.",
)
def search_policy(ctx, args):
    return {"policy": "Refund duplicate charges after verification."}


model = ScriptedModel([
    {
        "action": "tool",
        "tool_name": "search_policy",
        "arguments": {"question": "Can we refund a duplicate charge?"},
    },
    {
        "action": "final",
        "answer": {"decision": "refund_allowed", "reason": "duplicate charge policy matched"},
    },
])

with AgentHarness(os.environ["DATABASE_URL"], tenant_id=TENANT_ID, model_client=model) as harness:
    harness.migrate()
    harness.set_budget(max_model_calls=20, max_tool_executions=10, max_estimated_cost_usd=5)
    harness.register_tool(search_policy)

    run = harness.run({"question": "Can we refund a duplicate charge?"})

    print(run.status)
    print(run.answer)
    print(run.explain())
```

Run it:

```bash
python quickstart.py
```

Expected shape:

```text
completed
{'decision': 'refund_allowed', 'reason': 'duplicate charge policy matched'}
events include: model_call_reserved, model_call_completed, tool_started, tool_completed, run_completed
```

The important part is not the Python wrapper. The wrapper writes and reads the same database rows that the CLI, SQL functions, workers, and console use.

## Live Model Adapter

For OpenAI:

```bash
.venv/bin/python -m pip install -e '.[openai]'
export OPENAI_API_KEY=...
```

```python
from rowplane.adapters import OpenAIModelClient

model = OpenAIModelClient(
    model="gpt-5",
    max_output_tokens=512,
    estimated_call_cost_usd=0.01,
)
```

The adapter only supplies model text. Rowplane still parses one command and Postgres still validates permissions, schemas, approvals, budgets, idempotency, and state transitions.

## CLI

New projects should use `rowplane`, `rowplane` imports, and `ROWPLANE_DATABASE_URL`.

Legacy `pg-agent`, `pg_agent`, and `PG_AGENT_DATABASE_URL` compatibility still works.

`register-tool` writes the tool contract to Postgres. Actual Python handlers run inside workers or scripts that import and register the handler, like the quick-start example above.

```bash
.venv/bin/rowplane --database-url "$DATABASE_URL" set-budget \
  --tenant-id "$TENANT_ID" \
  --max-model-calls 1000 \
  --max-tool-executions 500 \
  --max-estimated-cost-usd 25

.venv/bin/rowplane --database-url "$DATABASE_URL" register-tool \
  --tenant-id "$TENANT_ID" \
  --name search_policy

.venv/bin/rowplane --database-url "$DATABASE_URL" run \
  --tenant-id "$TENANT_ID" \
  --task-json '{"question":"Which policy applies?"}'

.venv/bin/rowplane --database-url "$DATABASE_URL" explain \
  --tenant-id "$TENANT_ID" "$RUN_ID"
```

## Parallel Workers

For local demos, `harness.run(...)` is fine.

For production-style processing, run multiple workers against the same Postgres database. Workers claim leased work, execute only approved I/O, heartbeat while active, and release or complete the lease when done.

That gives Rowplane the normal backend scaling shape: one durable database control plane, PgBouncer for connections, and N stateless workers.

See [Scaling Notes](docs/SCALING.md) before making production scale claims.

## Showcase

Run the Postgres use cases:

```bash
docker compose up --build postgres-use-cases
```

The showcase covers retrieval, approvals, memory, permission denial, multi-agent delegation, SQL schema guardrails, SRE rollback, state-diff evals, customer support resolution with leased workers, tenant isolation, final-answer contracts, and trajectory replay.

## Management Console

```bash
docker compose up --build management-api
```

Open:

```text
http://localhost:8000/console
```

The console is a thin client over the same Postgres control plane.

## Benchmarks

The benchmark is scoped to Rowplane's niche: Postgres-native governance, approvals, replay/search, tenant boundaries, and audit evidence.

See [benchmarks](benchmarks/README.md) and [the benchmark report](benchmarks/reports/usefulness_benchmark.md).

## Tests

```bash
.venv/bin/python -B -m unittest discover -s tests
```

The suite validates state transitions, events, command parsing, SQL validation, final-answer contracts, tool schemas, approval policies, permissions, idempotency, multi-agent handoffs, memory filters, eval results, trajectory replay, search, management API behavior, and real Postgres use cases.
