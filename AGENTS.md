# AGENTS.md — Postgres-Native Agent Harness

## Mission

Build a frameworkless, SQL-native, Postgres-first AI agent harness.

Postgres is the control plane.
Workers execute.
The LLM proposes the next action.
The database validates, records, governs, queues, evaluates, replays, searches, and audits everything.

Do not introduce LangChain, LangGraph, CrewAI, Temporal, Redis, Kafka, or other orchestration frameworks unless explicitly requested.

This project is not trying to compete with agent frameworks. The niche is:

```text
A Postgres-native harness for governed agent execution.
A SQL-native runtime ledger for reliable, auditable AI agents.
Build agents however you want; govern their execution in Postgres.
```

Agreed product and runtime decisions must be reflected in this file as the project contract evolves.

## Developer Adoption Principle

Reduce adoption friction without weakening the Postgres-native contract. The beginner path may use a small Python facade, decorators, CLI commands, and starter examples, but those conveniences must still persist and inspect the same database rows.

The intended learning path is:

```text
Beginner: AgentHarness, @tool, rowplane CLI
Intermediate: agent_events, approvals, tool_executions, eval_results
Advanced: SQL runtime functions, RLS, PGMQ, replay, search, custom governance
```

Convenience APIs must not become an orchestration framework. They are adapters over Postgres, not replacements for Postgres as the control plane.

---

## Core Mental Model

Keep the system simple:

```text
agent_runs   = current state
agents       = specialist definitions
agent_tasks  = bounded multi-agent work items
agent_messages = delegation and result handoffs
agent_events = append-only truth
agent_tools  = allowed actions
agent_memory = knowledge
PGMQ         = wake-up queue
worker       = interpreter
LLM          = next-action proposer
```

The agent is a state machine over SQL rows.

Smarter models may make smarter decisions, but their freedom is bounded by data-driven contracts: command schema, tool schemas, approval policies, tenant filters, final-answer contracts, max iterations, and append-only events. Prefer dynamic contracts stored in run/task/tool data over hard-coded orchestration logic.

---

## Core Tables

The minimum viable schema should include:

```text
agent_runs
agent_events
agent_tools
agent_tool_permissions
agent_memory
tool_executions
approval_requests
eval_cases
eval_results
agents
agent_tasks
agent_messages
```

Optional later:

```text
prompts
guardrail_rules
guardrail_events
agent_metrics
audit_events
trajectory_snapshots
```

Prefer fewer tables early. Use `agent_events` as the universal trace log.

---

## Required Postgres Extensions

Use these by default:

```sql
pgvector
pgmq
pg_cron
pgcrypto
```

Optional:

```sql
pgaudit
timescaledb
pg_stat_statements
```

---

## Implementation Rules

1. Every important action must be written to `agent_events`.
2. Do not keep important agent state only in memory.
3. The LLM must never directly execute tools, SQL, shell commands, or external actions.
4. The LLM may only return a structured command.
5. The worker rejects invalid JSON, and Postgres validates every normalized command before execution.
6. All tools must be registered in `agent_tools`.
7. Tool permissions must be checked before execution.
8. Tool input schemas must be checked before execution and output schemas before accepting results.
9. Risky tools must create an approval request instead of executing immediately; prefer declarative approval policies where risk depends on arguments.
10. All side-effecting tools must be idempotent.
11. All runs must have a max iteration limit.
12. Every run must end as `completed`, `failed`, or `blocked`.
13. If a final answer contract exists, invalid final answers must be rejected with an event and corrected within max iterations.
14. Use migrations for schema changes.
15. Do not store secrets in Postgres tables, logs, events, or test fixtures.
16. Always include `tenant_id` on tenant-scoped data.
17. Prefer Row-Level Security for multi-tenant enforcement.
16. Prefer database functions for harness decisions: command validation, state transitions, tool reservations, approvals, idempotency, event writes, replay, and search.
17. Workers may call LLMs and external tool handlers, but they must not bypass Postgres governance.

---

## Agent Command Contract

The LLM must return exactly one command.

Allowed commands:

```json
{
  "action": "final",
  "answer": {}
}
```

```json
{
  "action": "tool",
  "tool_name": "search_policy_documents",
  "arguments": {}
}
```

```json
{
  "action": "ask_human",
  "reason": "Approval required.",
  "payload": {}
}
```

```json
{
  "action": "remember",
  "memory_type": "case_learning",
  "content": "Useful memory text.",
  "metadata": {}
}
```

```json
{
  "action": "fail",
  "reason": "Cannot continue."
}
```

```json
{
  "action": "delegate",
  "to_agent": "policy_researcher",
  "task": {},
  "reason": "Need specialist evidence."
}
```

Reject malformed commands.

---

## State Machine

Keep the run lifecycle small:

```text
queued
thinking
needs_tool
tool_running
waiting_approval
evaluating
completed
failed
blocked
```

Allowed flow:

```text
queued -> thinking
thinking -> needs_tool
thinking -> waiting_approval
thinking -> completed
thinking -> failed
needs_tool -> tool_running
tool_running -> queued
waiting_approval -> queued
any -> failed
any -> blocked
```

Task lifecycle adds child coordination:

```text
thinking -> waiting_child
waiting_child -> queued
```

---

## Worker Loop

The worker should be boring and deterministic:

```text
1. Read message from PGMQ
2. Load agent_run
3. Load agent_events
4. Build prompt
5. Call model
6. Parse command
7. Submit normalized command to Postgres
8. Follow the database decision
9. Execute only approved/reserved external tool handlers
10. Queue next work if needed
```

No hidden orchestration logic.

---

## Tool Execution

Tools are not agent logic.

Tools are normal deterministic functions invoked by workers.

Required behavior:

```text
validate input
check permission
check approval requirement
reserve idempotently
write tool_started event
write tool_completed or tool_failed event
requeue run when complete
```

Use `tool_executions` for idempotency.

The database owns tool reservation. Workers only execute an external tool handler after Postgres returns an executable reservation.

---

## Memory

Use `agent_memory` with `pgvector`.

Memory must be:

```text
tenant-scoped
typed
timestamped
metadata-rich
auditable
```

Do not treat vector search as magic. Combine vector search with SQL filters.

---

## Evaluation

Evaluation is a first-class harness feature.

An eval is just another agent run with scoring.

Track:

```text
correctness
tool correctness
retrieval relevance
format compliance
latency
cost
human agreement
policy compliance
```

Store results in `eval_results`.

---

## SQL-Native Runtime API

The framework should mature toward a small SQL API that workers and management tools call:

```text
app.submit_agent_command(...)      = validate/record/apply LLM command
app.reserve_tool_execution(...)    = enforce tool schema, permission, idempotency, approval
app.complete_tool_execution(...)   = record external tool result/failure and requeue
app.resolve_approval_request(...)  = resolve human approval with event/state updates
app.search_harness(...)            = tenant-scoped trace/memory/tool/eval search
app.run_trajectory_v               = replay/debug read model
```

Do not move LLM calls, shell access, network calls, or arbitrary plugin execution into Postgres. Postgres decides what is allowed; workers perform external I/O.

---

## Testing Expectations

Every change should include tests.

Minimum test coverage:

```text
state transitions
event writes
database command validation
tool permission checks
approval flow
malformed LLM command rejection
idempotent tool execution
memory search filters
eval result creation
trajectory replay
tenant-scoped harness search
```

Use integration tests for Postgres behavior where possible.

---

## Code Style

Prefer:

```text
small modules
explicit SQL
clear migrations
typed inputs
structured errors
boring workers
simple state transitions
```

Avoid:

```text
magic abstractions
hidden global state
implicit retries
unbounded loops
agent framework dependencies
business logic inside prompts
raw LLM access to tools
```

---

## Suggested Repo Layout

```text
/db
  /migrations
  /seeds

/src
  /runtime
  /workers
  /tools
  /memory
  /evals
  /approvals
  /db

/tests
/docs
```

---

## Definition of Done

A task is complete only when:

```text
schema changes have migrations
new behavior writes agent_events
state transitions are explicit
tool permissions are enforced
tests pass
dangerous actions are idempotent or approval-gated
tenant_id is respected
no secrets are logged
README or docs are updated if behavior changes
```

---

## Product Positioning

This project is not another agent framework.

It is:

```text
A SQL-first agent harness for reliable, auditable AI agents.
```

Or:

```text
A Postgres-native control plane for governed enterprise AI agents.
```

The north star:

```text
One run table.
One event table.
One tool table.
One memory table.
One queue.
One worker loop.
```

## Roadmap

P0:

```text
database-enforced command schemas
database-enforced tool input schemas
database-owned tool reservations
database-owned approval resolution
database-owned idempotency decisions
workers as deterministic I/O adapters
```

P1:

```text
trajectory replay and debugging
Postgres-native search across memory, events, tools, evals, and audit
readiness/eval gates exposed through SQL and the console
```

P2:

```text
optional pg_jsonschema and pg_search support
multi-agent governance policies
SQL-first agent development APIs
optional adapters from other agent frameworks into this harness
```
