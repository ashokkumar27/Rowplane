# Postgres-Native Agent Harness Benchmark

Generated: `2026-06-02T18:38:50+00:00`
Model: `gpt-5.4-mini`

This benchmark measures whether a Postgres-native control plane adds practical value for governed, auditable agent work. It is not a generic agent-framework leaderboard.

## Summary

| System | Runs | Avg Score | Task | Control Plane | Ops | Pass Rate | Avg Latency | Est. Cost |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| rowplane | 15 | 77.67 | 20.33 | 34.33 | 23.0 | 0.733 | 2496.93 | $0.014520 |
| plain_openai_tool_loop | 15 | 49.13 | 12.2 | 16.93 | 20.0 | 0.000 | 1571.73 | $0.004153 |

## Scenario Results

### multi_agent_refund_review

| System | Repeat | Score | Errors |
| --- | ---: | ---: | --- |
| rowplane | 1 | 78.0 |  |
| rowplane | 2 | 78.0 |  |
| rowplane | 3 | 70.0 |  |
| plain_openai_tool_loop | 1 | 33.0 |  |
| plain_openai_tool_loop | 2 | 33.0 |  |
| plain_openai_tool_loop | 3 | 33.0 |  |

### permission_denied_safety

| System | Repeat | Score | Errors |
| --- | ---: | ---: | --- |
| rowplane | 1 | 83.0 |  |
| rowplane | 2 | 83.0 |  |
| rowplane | 3 | 83.0 |  |
| plain_openai_tool_loop | 1 | 65.0 |  |
| plain_openai_tool_loop | 2 | 65.0 |  |
| plain_openai_tool_loop | 3 | 65.0 |  |

### policy_retrieval_qa

| System | Repeat | Score | Errors |
| --- | ---: | ---: | --- |
| rowplane | 1 | 79.0 |  |
| rowplane | 2 | 79.0 |  |
| rowplane | 3 | 60.0 |  |
| plain_openai_tool_loop | 1 | 47.0 |  |
| plain_openai_tool_loop | 2 | 47.0 |  |
| plain_openai_tool_loop | 3 | 47.0 |  |

### refund_approval

| System | Repeat | Score | Errors |
| --- | ---: | ---: | --- |
| rowplane | 1 | 58.0 |  |
| rowplane | 2 | 85.0 |  |
| rowplane | 3 | 85.0 |  |
| plain_openai_tool_loop | 1 | 53.0 |  |
| plain_openai_tool_loop | 2 | 40.0 |  |
| plain_openai_tool_loop | 3 | 53.0 |  |

### tenant_memory_search

| System | Repeat | Score | Errors |
| --- | ---: | ---: | --- |
| rowplane | 1 | 87.0 |  |
| rowplane | 2 | 87.0 |  |
| rowplane | 3 | 70.0 |  |
| plain_openai_tool_loop | 1 | 52.0 |  |
| plain_openai_tool_loop | 2 | 52.0 |  |
| plain_openai_tool_loop | 3 | 52.0 |  |

## Interpretation

- Rowplane average: `77.67`.
- Non-Rowplane runnable baseline average: `49.13`.
- If Rowplane wins, the evidence should come from SQL-enforced governance, auditability, replay/search, tenant controls, and durable run state.
- If Rowplane loses a scenario, treat it as a concrete product gap in prompt contract, API ergonomics, or harness behavior.
- Do not present these numbers as a generic LangGraph/CrewAI/LangChain leaderboard unless native adapters are implemented for those frameworks.

## Framework Positioning

Comparing with other agent frameworks is useful for positioning, not as a default scored leaderboard. Rowplane should be evaluated against its niche: SQL-native governance and auditability.

| System | Primary Job | Strongest When | Postgres-Native Fit | Benchmark Role |
| --- | --- | --- | --- | --- |
| rowplane | Postgres-native control plane for governed agent runs | Teams need SQL-visible state, approvals, replay, search, tenant boundaries, and audit evidence. | Native | Runnable system under test |
| plain_openai_tool_loop | Minimal LLM loop with deterministic Python tools | Teams want the simplest possible baseline and accept process-local state. | None | Runnable baseline |
| LangGraph | Stateful graph orchestration with checkpointing and human-in-the-loop patterns | Teams need explicit graph control flow, durable checkpoints, and time-travel debugging. | Possible through custom persistence, not the product center | Positioning comparison unless a native adapter is implemented |
| LangChain | Broad agent/tool integration ecosystem | Teams need many model, retriever, tool, and integration options quickly. | External integration | Positioning comparison unless a native adapter is implemented |
| CrewAI | Role-based crews, flows, collaboration, and operational agent automation | Teams model work as collaborative roles and tasks with flow orchestration. | External integration | Positioning comparison unless a native adapter is implemented |
| Pydantic AI | Typed Python agent development with evals and durable execution integrations | Teams prioritize typed interfaces, testability, evals, and Python-native ergonomics. | Can be paired with durable backends; not SQL control-plane first | Positioning comparison unless a native adapter is implemented |
| OpenAI Agents SDK | Lightweight SDK for agents, tools, handoffs, tracing, and OpenAI model integration | Teams want a direct vendor SDK with minimal abstraction and good tracing hooks. | External integration | Positioning comparison unless a native adapter is implemented |
| LlamaIndex | Data, retrieval, indexing, and knowledge-agent workflows | Teams are building retrieval-heavy agents over private data sources. | External integration | Positioning comparison unless a native adapter is implemented |

## Method

- Default scored systems are `rowplane` and `plain_openai_tool_loop`.
- `plain_openai_tool_loop` is the practical baseline: the same model and Python tools without a durable SQL control plane.
- Other frameworks are included as a positioning matrix unless native adapters are implemented for them.
- Scores weight functional correctness, governance, SQL/audit evidence, cost/latency, and developer effort.
- Rowplane is judged on its intended niche: Postgres-native control plane, approvals, replay/search, tenant evidence, and durable traceability.

## Sources

- [LangChain agents](https://docs.langchain.com/oss/python/langchain/agents)
- [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution)
- [CrewAI docs](https://docs.crewai.com/)
- [Pydantic AI agents](https://ai.pydantic.dev/agent/)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/running_agents/)
- [LlamaIndex agents](https://developers.llamaindex.ai/)
- [AgentBench](https://arxiv.org/abs/2308.03688)
- [GAIA](https://arxiv.org/abs/2311.12983)
