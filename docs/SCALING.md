# Scaling Notes

This file parks the current scalability assessment for future implementation and benchmarking.

Rowplane is designed to scale horizontally by keeping workers stateless and making Postgres the control plane for leases, state transitions, budgets, approvals, idempotency, queues, and events. Many agents and users can share the same runtime because work is claimed through database-owned leases instead of in-memory worker coordination.

The current posture is:

```text
Scalable foundation: yes.
Scale-proven production system: not yet.
```

## What Already Scales

- Multiple worker processes or containers can claim work from the same database.
- SQL leases use row locking and `SKIP LOCKED` semantics so workers can claim queued work concurrently.
- Unique active leases prevent two workers from processing the same run or task at the same time.
- Tenant-scoped data and indexes are part of the core schema.
- Runtime limits and budgets govern concurrent work, child tasks, model calls, tool executions, and estimated model cost.
- Tool executions are recorded idempotently.
- `agent_events` provides an append-only audit trail for replay, debugging, and evaluation.
- PgBouncer can reduce database connection pressure for many workers and API clients.

Agent count itself is not the main bottleneck. Agents are mostly definitions and policy rows. The pressure comes from active runs, task fan-out, event writes, model calls, tool latency, memory search, and user/API connection volume.

## Expected Scale Bands

These are positioning estimates, not benchmark claims.

- Small/internal use: dozens of agents and dozens to hundreds of concurrent runs should fit the current architecture with one Postgres instance, PgBouncer, and a few workers.
- Team/platform use: hundreds of agents and thousands to tens of thousands of runs per day should be plausible with worker autoscaling, retention, monitoring, and tuned indexes.
- Large SaaS use: requires partitioning, budget rollups, API auth hardening, benchmarked worker throughput, rate limits, and retention/archival before making strong claims.
- Very high volume: Postgres can remain the governed control plane, but event analytics, long-term history, and high-throughput search may need partitioning, read replicas, warehouse export, or other optional infrastructure.

## Current Bottlenecks To Watch

1. `agent_events` will grow fastest.

   Add partitioning by time and/or tenant before heavy production usage. Also define retention and archival policy.

2. Runtime budget usage can become expensive if it is repeatedly derived from ledger counts.

   Keep the ledger as truth, but add rollup counters or materialized usage tables for high-volume tenants.

3. Worker throughput is currently simple and conservative.

   The lease path is safe, but high throughput should add batched claims, worker pools, and better supervision metrics.

4. LLM providers may bottleneck before Postgres.

   Add provider rate-limit handling, retry/backoff policy, per-tenant model-call limits, and clear cost accounting.

5. PgBouncer transaction pooling needs care.

   Tenant context must be transaction-local or reliably reset so Row-Level Security and tenant-scoped functions remain correct.

6. Vector memory search needs production tuning.

   Tune vector indexes, combine vector search with SQL filters, and consider tenant/time partitioning for large memory tables.

## Future Implementation Work

Prioritize these before claiming large-scale production readiness:

1. Partition `agent_events`.
2. Add runtime usage rollups for budgets, cost, and active work telemetry.
3. Add batched lease claiming and worker pool execution.
4. Add benchmark scenarios for 100, 1,000, and 10,000 concurrent runs.
5. Track p50/p95/p99 claim latency, state transition latency, event insert latency, DB CPU/IO, lock waits, LLM latency, tool latency, and failed/reclaimed leases.
6. Document a production topology: Postgres primary, PgBouncer, management API, N workers, monitoring, backup/restore, retention, and optional read replicas.
7. Add provider-specific rate-limit and retry policies.
8. Add operational guidance for tenant budgets and noisy-neighbor protection.

## Positioning

Use careful language until load tests exist:

```text
Rowplane is designed for horizontally scalable, Postgres-governed agent execution.
Production scale depends on Postgres sizing, worker count, event retention, budget rollups, and provider limits.
```

Do not claim internet-scale or hyperscale readiness until the benchmark suite and production hardening work above are complete.
