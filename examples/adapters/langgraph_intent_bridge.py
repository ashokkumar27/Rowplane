"""Minimal LangGraph planner-only Rowplane intent bridge example.

The graph returns one RowplaneIntent. It does not bind Rowplane tools or execute
side effects; Rowplane validates policy and executes approved work.
"""

from __future__ import annotations

import json

from rowplane.adapters import LangGraphIntentClient


class DemoCompiledGraph:
    def invoke(self, state, **kwargs):
        return {
            "messages": [
                {
                    "role": "assistant",
                    "content": json.dumps(
                        {
                            "schema_version": 1,
                            "intent": "tool_request",
                            "tool_name": "search_policy_documents",
                            "arguments": {"query": "refund policy"},
                        }
                    ),
                }
            ]
        }


def build_model() -> LangGraphIntentClient:
    return LangGraphIntentClient(graph=DemoCompiledGraph())
