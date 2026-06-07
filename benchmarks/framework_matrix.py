"""Qualitative positioning matrix for adjacent agent frameworks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameworkPosition:
    name: str
    primary_job: str
    strongest_when: str
    postgres_native_fit: str
    benchmark_role: str


def framework_positions() -> list[FrameworkPosition]:
    return [
        FrameworkPosition(
            name="rowplane",
            primary_job="Postgres-native control plane for governed agent runs",
            strongest_when="Teams need SQL-visible state, approvals, replay, search, tenant boundaries, and audit evidence.",
            postgres_native_fit="Native",
            benchmark_role="Runnable system under test",
        ),
        FrameworkPosition(
            name="plain_openai_tool_loop",
            primary_job="Minimal LLM loop with deterministic Python tools",
            strongest_when="Teams want the simplest possible baseline and accept process-local state.",
            postgres_native_fit="None",
            benchmark_role="Runnable baseline",
        ),
        FrameworkPosition(
            name="LangGraph",
            primary_job="Stateful graph orchestration with checkpointing and human-in-the-loop patterns",
            strongest_when="Teams need explicit graph control flow, durable checkpoints, and time-travel debugging.",
            postgres_native_fit="Possible through custom persistence, not the product center",
            benchmark_role="Positioning comparison unless a native adapter is implemented",
        ),
        FrameworkPosition(
            name="LangChain",
            primary_job="Broad agent/tool integration ecosystem",
            strongest_when="Teams need many model, retriever, tool, and integration options quickly.",
            postgres_native_fit="External integration",
            benchmark_role="Positioning comparison unless a native adapter is implemented",
        ),
        FrameworkPosition(
            name="CrewAI",
            primary_job="Role-based crews, flows, collaboration, and operational agent automation",
            strongest_when="Teams model work as collaborative roles and tasks with flow orchestration.",
            postgres_native_fit="External integration",
            benchmark_role="Positioning comparison unless a native adapter is implemented",
        ),
        FrameworkPosition(
            name="Pydantic AI",
            primary_job="Typed Python agent development with evals and durable execution integrations",
            strongest_when="Teams prioritize typed interfaces, testability, evals, and Python-native ergonomics.",
            postgres_native_fit="Can be paired with durable backends; not SQL control-plane first",
            benchmark_role="Positioning comparison unless a native adapter is implemented",
        ),
        FrameworkPosition(
            name="OpenAI Agents SDK",
            primary_job="Lightweight SDK for agents, tools, handoffs, tracing, and OpenAI model integration",
            strongest_when="Teams want a direct vendor SDK with minimal abstraction and good tracing hooks.",
            postgres_native_fit="External integration",
            benchmark_role="Positioning comparison unless a native adapter is implemented",
        ),
        FrameworkPosition(
            name="LlamaIndex",
            primary_job="Data, retrieval, indexing, and knowledge-agent workflows",
            strongest_when="Teams are building retrieval-heavy agents over private data sources.",
            postgres_native_fit="External integration",
            benchmark_role="Positioning comparison unless a native adapter is implemented",
        ),
    ]
