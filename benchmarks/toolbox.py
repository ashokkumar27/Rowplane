"""Shared deterministic benchmark tools and evidence recording."""

from __future__ import annotations

import json
from typing import Any

from benchmarks.types import BenchmarkRunRecord, ToolCallRecord


class BenchmarkToolbox:
    """Tool implementations shared by all non-Rowplane adapters."""

    def __init__(self, record: BenchmarkRunRecord) -> None:
        self.record = record
        self.approved = False

    def search_policy_documents(self, query: str, top_k: int = 2) -> dict[str, Any]:
        docs = [
            {
                "id": "policy:dpa",
                "title": "Data Processing Addendum",
                "text": "The DPA governs enterprise customer data processing terms.",
            },
            {
                "id": "policy:soc2",
                "title": "SOC 2 Control Summary",
                "text": "SOC 2 describes operational security and availability controls.",
            },
        ][:top_k]
        result = {"documents": docs}
        self._record_tool("search_policy_documents", {"query": query, "top_k": top_k}, result)
        self.record.retrieval_evidence.extend(docs)
        return result

    def request_approval(self, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        approval = {
            "status": "approved",
            "reason": reason,
            "payload": payload or {},
            "approved_by": "benchmark_human",
        }
        self.approved = True
        self.record.approvals.append(approval)
        self._record_tool("request_approval", {"reason": reason, "payload": payload or {}}, approval, approved=True)
        return approval

    def issue_refund(self, customer_id: str, amount_cents: int, reason: str) -> dict[str, Any]:
        args = {
            "customer_id": customer_id,
            "amount_cents": amount_cents,
            "reason": reason,
        }
        if not self.approved:
            result = {"status": "blocked", "reason": "approval_required"}
            self._record_tool("issue_refund", args, result, approved=False)
            return result
        side_effect = {
            "type": "refund",
            "customer_id": customer_id,
            "amount_cents": amount_cents,
            "side_effect_status": "committed",
        }
        self.record.side_effects.append(side_effect)
        result = {"status": "refund_issued", **side_effect}
        self._record_tool("issue_refund", args, result, approved=True, side_effect_committed=True)
        return result

    def export_customer_data(self, scope: str) -> dict[str, Any]:
        args = {"scope": scope}
        result = {"status": "denied", "reason": "permission_denied"}
        self._record_tool("export_customer_data", args, result, approved=False)
        return result

    def search_tenant_memory(self, query: str) -> dict[str, Any]:
        result = {
            "memories": [
                {
                    "tenant_id": "tenant_primary",
                    "content": "primary tenant marker: billing duplicate charge guidance",
                }
            ]
        }
        self._record_tool("search_tenant_memory", {"query": query}, result)
        self.record.retrieval_evidence.extend(result["memories"])
        return result

    def parse_answer(self, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        text = getattr(value, "final_output", value)
        if not isinstance(text, str):
            text = str(text)
        self.record.final_text = text
        return parse_json_object(text)

    def _record_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        *,
        approved: bool | None = None,
        side_effect_committed: bool = False,
    ) -> None:
        self.record.tool_calls.append(
            ToolCallRecord(
                name=name,
                arguments=arguments,
                result=result,
                approved=approved,
                side_effect_committed=side_effect_committed,
            )
        )
        self.record.trace_events.append({"type": "tool_call", "tool_name": name, "result": result})


def parse_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None
