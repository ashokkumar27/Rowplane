# Postgres-Native Agent Harness Benchmark

This benchmark answers one question:

> Does Rowplane provide practical value as a Postgres-native control plane for governed, auditable agent work?

It is not a generic LangGraph/CrewAI/LangChain leaderboard. Those frameworks solve broader orchestration, agent ergonomics, retrieval, typed-agent, or SDK problems. Rowplane's niche is SQL-native state, approval, replay, search, tenant boundaries, and audit evidence.

## Default Scored Benchmark

Default runnable systems:

- `rowplane`: real Postgres control plane, migrations, tool catalog, approval rows, event trace, replay/search evidence.
- `plain_openai_tool_loop`: same live model and deterministic Python tools, but no durable SQL control plane.

Run a listing:

```bash
python -m benchmarks.run --list
```

Install default benchmark dependency:

```bash
python -m pip install -r benchmarks/requirements.txt
```

Run live benchmark:

```bash
docker compose up -d postgres
OPENAI_API_KEY=... \
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane \
  python -m benchmarks.run --model gpt-5.4-mini --repeats 3 --max-usd 5
```

Outputs:

- `benchmarks/results/latest.json`
- `benchmarks/reports/usefulness_benchmark.md`

## Optional Experimental Wrappers

The other framework wrappers are smoke tests only. They do not use full native LangGraph, CrewAI, LangChain, Pydantic AI, OpenAI Agents SDK, or LlamaIndex implementations. Do not publish those scores as a framework leaderboard.

Install optional dependencies:

```bash
python -m pip install -r benchmarks/requirements-frameworks.txt
```

Run exploratory wrappers:

```bash
python -m benchmarks.run --include-experimental-frameworks --repeats 1 --max-usd 5
```

## What Good Looks Like

A useful Rowplane result should show concrete SQL evidence for:

- Durable run state.
- Append-only events.
- Tool registration and permission checks.
- Approval gating before side effects.
- Tenant-scoped memory/search.
- Replay/debug timeline.
- Idempotent side-effect execution.

The framework positioning matrix in the report explains where other agent frameworks fit without pretending Rowplane replaces them.
