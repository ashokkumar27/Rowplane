# Refund Agent Starter

This starter shows the low-friction developer path while preserving the Postgres-native control plane.

For the full step-by-step walkthrough, start with [../../../docs/TUTORIAL.md](../../../docs/TUTORIAL.md). Use [../../../docs/REFERENCE.md](../../../docs/REFERENCE.md) when you need API, CLI, SQL runtime, or management details. This starter is the runnable code companion for the tutorial.

It demonstrates:

- `@tool` with a Pydantic input model.
- `AgentHarness.migrate()` for schema setup.
- `AgentHarness.register_tool()` creating `agent_tools` and `agent_tool_permissions`.
- `AgentHarness.run()` creating `agent_runs`, queueing PGMQ work, and writing `agent_events`.
- Approval inspection and resolution through `approval_requests`.
- `run.explain()` as the first debugging surface.

Run with a disposable Postgres database that has the required extensions:

```bash
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane \
  python -B examples/starters/refund_agent/agent.py
```

The scripted model is deterministic so the starter tests harness behavior rather than model quality.
