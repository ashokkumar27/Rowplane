"""Plain OpenAI tool-loop benchmark baseline."""

from __future__ import annotations

from benchmarks.adapters.base import PortableToolLoopAdapter


class PlainOpenAIToolLoopAdapter(PortableToolLoopAdapter):
    """A minimal live LLM + Python tool loop with no durable control plane.

    This is the useful baseline for Rowplane: it shows what the model and
    deterministic tools can do without Postgres enforcing state, approval,
    idempotency, tenant evidence, replay, or SQL auditability.
    """

    name = "plain_openai_tool_loop"
    package_name = "openai"
