"""Competitor framework benchmark adapters."""

from __future__ import annotations

from benchmarks.adapters.base import PortableToolLoopAdapter


class LangChainAdapter(PortableToolLoopAdapter):
    name = "langchain"
    package_name = "langchain"


class LangGraphAdapter(PortableToolLoopAdapter):
    name = "langgraph"
    package_name = "langgraph"


class CrewAIAdapter(PortableToolLoopAdapter):
    name = "crewai"
    package_name = "crewai"


class PydanticAIAdapter(PortableToolLoopAdapter):
    name = "pydantic_ai"
    package_name = "pydantic_ai"


class OpenAIAgentsAdapter(PortableToolLoopAdapter):
    name = "openai_agents"
    package_name = "agents"


class LlamaIndexAdapter(PortableToolLoopAdapter):
    name = "llamaindex"
    package_name = "llama_index"
