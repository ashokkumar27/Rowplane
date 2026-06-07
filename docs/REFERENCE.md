# Reference

This is the compact technical reference for Rowplane. Start with `README.md`, learn the full flow in `docs/TUTORIAL.md`, then use this file when you need exact surfaces.

## Mental Model

```text
agent_runs      = current run state
agents          = specialist definitions
agent_tasks     = bounded child work
agent_messages  = delegation/result handoffs
agent_events    = append-only truth
agent_tools     = allowed actions
agent_memory    = knowledge
PGMQ            = wake-up queue
worker          = interpreter
LLM             = next-action proposer
```

The model proposes exactly one structured command. The worker parses it. Postgres validates, records, governs, queues, evaluates, replays, searches, and audits.

## Core Tables

- `agent_runs`: run lifecycle, task input, answer, error, iteration count, model.
- `agent_events`: append-only run trace.
- `agent_tools`: tenant-scoped tool catalog, input schema, output schema, and approval policy.
- `agent_tool_permissions`: tenant, agent, user, or run allow/deny rules.
- `tool_executions`: idempotent tool execution records and results.
- `approval_requests`: approval gates for risky tools.
- `agent_memory`: typed tenant-scoped memory with optional `pgvector` embedding.
- `eval_cases`, `eval_results`: first-class evaluation inputs and scores.
- `agents`, `agent_tasks`, `agent_messages`: Postgres-native multi-agent coordination.
- `agent_task_dependencies`: explicit fan-out/fan-in dependencies between parent and child tasks.
- `agent_runtime_budgets`: scoped runtime caps for active work, child tasks, model calls, tool executions, and estimated model-call cost.
- `audit_events`: append-only management actions not tied to a run.

Required extensions: `pgvector`, `pgmq`, `pg_cron`, `pgcrypto`.

## State And Worker Loop

Run lifecycle:

```text
queued -> thinking
thinking -> needs_tool | waiting_approval | completed | failed
needs_tool -> tool_running
tool_running -> queued
waiting_approval -> queued
any -> failed | blocked
```

Task lifecycle adds `waiting_child -> queued`.

Worker loop:

1. Read a PGMQ message.
2. Set `app.tenant_id`.
3. Load run/task state.
4. Claim `queued -> thinking`.
5. Reserve the model call through Postgres budget policy.
6. Load trace events/messages for prompt context.
7. Call the model only after reservation succeeds.
8. Parse one command.
9. Let Postgres validate/reserve/update.
10. Execute registered Python handlers only after database approval.
11. Write events and requeue when needed.

Every run status change writes `run_status_changed`. Every task status change writes `task_status_changed`. `agent_events` and `agent_messages` are append-only.

## Python API

Import the developer facade:

```python
from rowplane import AgentHarness, tool
```

Optional OpenAI adapter:

```python
from rowplane.adapters import OpenAIModelClient

model = OpenAIModelClient(
    model="gpt-5",
    max_output_tokens=512,
    estimated_call_cost_usd=0.01,
    input_cost_per_million=2.0,
    output_cost_per_million=8.0,
    request_options={"metadata": {"app": "rowplane"}},
)
```

Install it with `pip install -e '.[openai]'`. The OpenAI SDK is imported only when no custom `client` is injected, so tests and non-OpenAI deployments do not need the dependency.

Optional OpenAI Agents SDK bridge:

```python
from rowplane.adapters import OpenAIAgentsCommandClient

model = OpenAIAgentsCommandClient(model="gpt-5.4-mini")
```

Install it with `pip install -e '.[openai-agents]'`. The bridge uses Agents as a command proposer only; Rowplane still validates and executes tools through Postgres governance.

Common methods:

```text
AgentHarness(database_url, tenant_id=..., model_client=...)
OpenAIModelClient(model='gpt-5', max_output_tokens=..., estimated_call_cost_usd=..., input_cost_per_million=..., output_cost_per_million=..., request_options=...)
OpenAIAgentsCommandClient(model='gpt-5.4-mini', agent=..., runner=..., run_config=..., max_turns=...)
AgentHarness.from_connection(conn, tenant_id=..., model_client=..., registry=...)
harness.migrate()
harness.register_tool(handler)
harness.register_tool_catalog(name, input_schema=..., output_schema=..., approval_policy=...)
harness.grant_tool(tool_name, subject_type='tenant', subject_id=None, allowed=True)
harness.register_agent(name, role=..., instructions=...)
harness.set_budget(max_model_calls=..., max_tool_executions=..., max_estimated_cost_usd=..., max_active_work=...)
harness.get_budget()
harness.create_run(task, model='sample-scripted-model', max_iterations=8, answer_contract=..., queue=True, required_capabilities=[...], priority=0, not_before=..., deadline_at=...)
harness.run(task, drain=True, max_steps=20)
harness.drain_run(run_id)
harness.drain_leased_work(worker_id='worker-1', max_steps=20, capabilities=[...], kinds=['task','run'])
harness.run_until_terminal(run_id, approval_handler=..., max_approval_cycles=5)
harness.events(run_id)
harness.approvals(run_id)
harness.tool_executions(run_id)
harness.trajectory(run_id)
harness.replay(run_id)
harness.search(query)
harness.search_memory(query=..., memory_type=..., metadata_contains=..., record_event_for_run_id=...)
harness.approve(approval_id, resolved_by=...)
harness.reject(approval_id, resolved_by=...)
harness.explain(run_id)
```

Tool definition:

```python
@tool(
    input_schema=RefundInput,
    output_schema={"type": "object", "required": ["status"]},
    is_side_effecting=True,
    approval_policy={"rules": [{"field": "amount_cents", "operator": "gte", "value": 10000}]},
)
def issue_refund(ctx, args):
    return {"status": "issued"}
```

`input_schema` and `output_schema` may be JSON-schema mappings or Pydantic model classes. Input schemas are enforced before execution; output schemas are enforced before results are accepted. `approval_policy` is JSON data stored in `agent_tools` and can require approval dynamically from arguments, for example amount thresholds or high-risk tools.

Run final-answer contracts are optional and data-driven:

```python
run = harness.create_run(
    {"question": "Should we refund?"},
    answer_contract={
        "schema": {"type": "object", "required": ["decision", "evidence_tools"]},
        "required_tools": ["search_policy_documents"],
        "must_reference_tools": True,
        "required_approval_status": "approved",
    },
)
```

If a model returns an invalid `final`, the worker writes `final_answer_rejected`, requeues the run, and gives the model a chance to correct the answer within `max_iterations`.

## CLI

Use `DATABASE_URL` or pass `--database-url`.

```bash
rowplane --database-url "$DATABASE_URL" migrate
rowplane --database-url "$DATABASE_URL" register-tool --tenant-id "$TENANT_ID" --name issue_refund --side-effecting \
  --schema-json '{"type":"object","required":["amount_cents"]}' \
  --output-schema-json '{"type":"object","required":["status"]}' \
  --approval-policy-json '{"rules":[{"field":"amount_cents","operator":"gte","value":10000}]}'
rowplane --database-url "$DATABASE_URL" register-agent --tenant-id "$TENANT_ID" --name planner --role planner --instructions 'Plan safely.'
rowplane --database-url "$DATABASE_URL" set-budget --tenant-id "$TENANT_ID" --max-model-calls 1000 --max-tool-executions 500 --max-estimated-cost-usd 25
rowplane --database-url "$DATABASE_URL" run --tenant-id "$TENANT_ID" --task-json '{"request":"Refund duplicate charge"}' \
  --answer-contract-json '{"schema":{"type":"object","required":["decision"]}}'
rowplane --database-url "$DATABASE_URL" events --tenant-id "$TENANT_ID" "$RUN_ID"
rowplane --database-url "$DATABASE_URL" trajectory --tenant-id "$TENANT_ID" "$RUN_ID"
rowplane --database-url "$DATABASE_URL" replay --tenant-id "$TENANT_ID" "$RUN_ID"
rowplane --database-url "$DATABASE_URL" explain --tenant-id "$TENANT_ID" "$RUN_ID"
rowplane --database-url "$DATABASE_URL" search --tenant-id "$TENANT_ID" "approval rejected"
rowplane --database-url "$DATABASE_URL" search-memory --tenant-id "$TENANT_ID" --query "refund" --memory-type case_learning
rowplane --database-url "$DATABASE_URL" approve --tenant-id "$TENANT_ID" "$APPROVAL_ID" --by human_1
rowplane --database-url "$DATABASE_URL" reject --tenant-id "$TENANT_ID" "$APPROVAL_ID" --by human_1
```

The CLI reads and writes the same Postgres rows as the Python API. The legacy `pg-agent` command and `PG_AGENT_DATABASE_URL` environment variable remain supported for compatibility; prefer `rowplane` and `ROWPLANE_DATABASE_URL` for new usage.

`drain_run` intentionally stops at `waiting_approval`. Use `run_until_terminal` when an application wants to provide an explicit approval policy callback and continue through one or more approval cycles until the run reaches `completed`, `failed`, or `blocked`.

`drain_leased_work` uses `app.claim_agent_work(...)` instead of reading one PGMQ message directly. It is the preferred path for horizontally scaled stateless workers because Postgres creates and closes `agent_work_leases` around each claimed run or task.

## SQL Runtime

Main SQL functions in `app`:

- `app.validate_agent_command(command, allow_delegate)`: validates allowed command shapes.
- `app.jsonb_matches_schema(value, schema)`: conservative JSON-schema subset validation.
- `app.submit_agent_command(...)`: records model commands and applies non-tool decisions.
- `app.reserve_model_call(...)`: checks scoped model-call and projected-cost budgets, then writes reservation or denial evidence before external LLM access.
- `app.complete_model_call(...)`: records model-call success/failure, latency, token counts, and estimated cost.
- `app.runtime_cost_budget_allows(...)`: checks projected model spend against scoped `max_estimated_cost_usd` budgets.
- `app.reserve_tool_execution(...)`: enforces registration, schema, permissions, approvals, and idempotency.
- `app.complete_tool_execution(...)`: validates output schema, records tool result/failure, writes events, and requeues.
- `app.resolve_approval_request(...)`: resolves approvals with event/state updates.
- `app.approval_policy_requires_approval(policy, arguments)`: evaluates catalog approval policy JSON against proposed tool arguments.
- `app.send_agent_wakeup(...)`: queues run/task work through PGMQ.
- `app.run_trajectory_v`: SQL replay/debug timeline.
- `app.search_harness(tenant_id, query, limit)`: tenant-scoped search across events, memory, tools, and evals.
- `app.claim_agent_work(...)`: atomically claims queued run/task work by creating durable worker leases with tenant concurrency limits.
- `app.heartbeat_agent_work(...)`: extends an active worker lease.
- `app.complete_agent_work(...)`: completes, releases, or fails an active worker lease and records the event.
- `app.expire_agent_work_leases(...)`: marks expired active leases and records recovery evidence.
- `app.create_task_dependency(...)`: records a required or optional child-task dependency for fan-in coordination.
- `app.complete_task_dependencies_for_child(...)`: satisfies or fails child dependencies, then releases or blocks waiting parents.
- `app.runtime_budget_allows(...)`: checks scoped budget rows and records budget decisions or denials.
- `app.runtime_budget_scope_usage(...)`: derives budget usage from existing runtime ledger rows.

Scheduler tables:

- `agent_work_leases`: durable active/completed/released/expired/failed work leases for stateless workers.
- `agent_runtime_limits`: optional tenant-level caps for concurrent total, run, and task work.

Scheduler policy columns on `agent_runs` and `agent_tasks`:

- `required_capabilities`: text array that must be contained in the claiming worker capability set.
- `priority`: higher values claim first.
- `not_before`: work is not claimable before this timestamp.
- `deadline_at`: tie-breaker after priority; earlier deadlines claim first.

Runtime budgets:

- The default developer path is one tenant-wide budget through `harness.set_budget(...)` or `Rowplane set-budget`.
- Advanced SQL users may still scope budgets to `tenant`, `run`, `task`, or `agent` through `agent_runtime_budgets`.
- `max_active_work` is enforced during SQL work claiming.
- `max_child_tasks` is enforced before delegation creates another child task.
- `max_model_calls` is enforced by `app.reserve_model_call(...)`; allowed calls write `model_call_reserved`, and denied calls write `model_call_denied_by_budget` before the worker can call an external model client.
- `max_estimated_cost_usd` is enforced from completed model-call cost events plus the next projected call cost. Successful calls write `model_call_completed`; failed external calls write `model_call_failed`.
- Budget checks write `runtime_budget_checked` when a matching budget allows work and `runtime_budget_exceeded` when a cap denies work. Delegation denials also write `delegation_rejected_by_budget`.

Schema subset: object/array/scalar types, required fields, property types, enum, const, numeric min/max, string minLength, array items, and `additionalProperties: false`.

## Governance

- Tools must exist in both local `ToolRegistry` and database `agent_tools`.
- Permission precedence is run-specific, then agent-specific, then tenant-wide.
- Side-effecting tools use `tool_executions.idempotency_key` derived from tenant, run, tool name, and canonical redacted arguments.
- `requires_approval` tools create `approval_requests` before the handler runs.
- Approved work re-enters `queued`; rejected work becomes `blocked`.
- Secrets are redacted before event/result persistence.

## Memory, Evals, Replay, Search

Memory is tenant-scoped, typed, timestamped, metadata-rich, and optionally vectorized. `harness.search_memory` combines lexical full-text search, metadata filters, source-run filters, type filters, and optional vector ordering; never treat vector search as tenant isolation. When tied to a run, it writes `memory_search_performed` evidence.

Eval results are rows in `eval_results` and emit `eval_result_created` events. Current scoring examples include correctness, tool correctness, retrieval relevance, format compliance, policy compliance, latency, cost, and human agreement.

Replay uses `app.run_trajectory_v` and `harness.replay`, returning run state, timeline, events, tool executions, approvals, and run memory. Search uses `app.search_harness`. Both are tenant-scoped and exposed through the management API.

## Multi-Agent

Multi-agent support stays in Postgres:

```text
agents = role/instructions/model
agent_tasks = parent/child work state
agent_messages = delegation and task-result handoffs
```

A `delegate` command creates a child task, a delegation message, and an explicit `agent_task_dependencies` row. Child completion writes a task-result message, satisfies or fails dependencies through SQL, and only requeues the parent when all required child dependencies are satisfied. Required child failures block the waiting parent by default. Task tool calls still go through `app.reserve_tool_execution`, including agent-specific permissions and task-scoped approvals.

Study `examples/use_cases/multi_agent_refund_review.py` for the planner -> researcher -> operator -> critic flow.

## Management API And Console

Run:

```bash
docker compose up --build management-api
```

Open:

```text
http://localhost:8000/console
```

Every tenant-scoped API request needs `X-Tenant-ID`. Mutations may include `X-Actor`.

Main endpoints:

```text
GET  /api/metrics/overview
GET  /api/approvals
GET  /api/approvals/{id}
POST /api/approvals/{id}/approve
POST /api/approvals/{id}/reject
GET  /api/runs
GET  /api/runs/{id}
GET  /api/runs/{id}/timeline
GET  /api/runs/{id}/trajectory
GET  /api/search?q=...
POST /api/runs/{id}/retry
GET  /api/tools
PATCH /api/tools/{id}
GET  /api/agents
GET  /api/agents/{id}
GET  /api/evals
GET  /api/evals/{id}/results
GET  /api/audit/events
GET  /api/memory
```

Console screens cover overview metrics, approvals, runs, tools, agents, evals, audit events, memory, trajectory replay, and harness search.

## Docker And Examples

Run the real Postgres showcase:

```bash
docker compose up --build postgres-use-cases
```

It runs twelve scenarios:

```text
policy_retrieval_qa
refund_approval
case_learning_memory
permission_denied_safety
multi_agent_refund_review
sql_schema_guardrail
sre_rollback_approval
enterprise_state_diff_ticket
customer_support_resolution
tenant_boundary_search_isolation
trajectory_replay_debug
final_answer_contract
```

Expected assessment includes `sample_pass_rate: 1.0`. The model is scripted for deterministic tests; production still needs a real model adapter, worker supervision, deployment lifecycle, metrics, and backup/restore practices.

The customer support starter can also run the same Postgres-governed flow with `--live --model gpt-5`. Live workers include registered tool contracts in prompt state and still rely on Postgres for schema rejection, approvals, idempotency, leases, budgets, memory, and final-answer validation.

Example map:

```text
examples/starters/refund_agent/              copyable refund starter
examples/starters/customer_support_agent/    copyable customer support starter
examples/use_cases/shared.py                 @tool handlers and scoring helpers
examples/use_cases/suite.py                  migration/seed/scenario orchestration
examples/use_cases/*.py                      one scenario per capability
```

## Tests

Run everything against Docker Postgres:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
docker compose up -d postgres
.venv/bin/python -B -m unittest discover -s tests
```

No tests should be intentionally skipped.
