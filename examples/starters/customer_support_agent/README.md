# Customer Support Agent Starter

This starter shows a realistic support workflow while keeping Rowplane's Postgres-native control plane visible.

It demonstrates:

- Normal Python `@tool` handlers for account lookup, policy search, refund, ticket creation, and case update.
- A single tenant-wide budget with `harness.set_budget(...)`.
- SQL-leased work with `harness.create_run(...)` plus `harness.drain_leased_work(...)`.
- Approval gating before the refund handler executes.
- Idempotent side effects recorded in `tool_executions`.
- Case learning written to `agent_memory`.
- `run.explain()` as the first debugging surface.

Run with a disposable Postgres database that has the required extensions:

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane \
  python -B examples/starters/customer_support_agent/agent.py
```

The scripted model is deterministic so developers can assess Rowplane behavior without model randomness. Swap in `OpenAIModelClient` later; the same Postgres rows, approvals, leases, events, tools, and memory remain the control plane.

Run the same workflow with a live OpenAI model:

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane \
OPENAI_API_KEY=... \
  python -B examples/starters/customer_support_agent/agent.py --live --model gpt-5
```

Live mode defaults to `--max-output-tokens 2400`. The worker includes the registered tool contracts in the prompt, so the model can see exact tool schemas while Postgres still rejects invalid arguments, approval-gates refunds, and enforces the final answer contract.
