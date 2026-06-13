"""Minimal Deep Agents planner-only Rowplane intent bridge example.

Pass an existing Deep Agents agent into the adapter. The agent returns one
RowplaneIntent; Rowplane remains the policy and execution authority.
"""

from __future__ import annotations

import json

from rowplane.adapters import DeepAgentsIntentClient


class DemoDeepAgent:
    def invoke(self, state, **kwargs):
        return {
            "structured_response": {
                "schema_version": 1,
                "intent": "final_answer",
                "answer": {"status": "ready_for_rowplane_validation"},
            }
        }


def build_model() -> DeepAgentsIntentClient:
    return DeepAgentsIntentClient(agent=DemoDeepAgent())
