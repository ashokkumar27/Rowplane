"""Evaluation result recording."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class EvalRepository(Protocol):
    def create_eval_result(
        self,
        tenant_id: str,
        eval_case_id: str,
        run_id: str,
        scores: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    def append_event(
        self,
        tenant_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        actor: str = "worker",
    ) -> None: ...


@dataclass(frozen=True)
class EvalScores:
    correctness: float | None = None
    tool_correctness: float | None = None
    retrieval_relevance: float | None = None
    format_compliance: float | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    human_agreement: float | None = None
    policy_compliance: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = dict(self.extra)
        for key in (
            "correctness",
            "tool_correctness",
            "retrieval_relevance",
            "format_compliance",
            "latency_ms",
            "cost_usd",
            "human_agreement",
            "policy_compliance",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = value
        return payload


class EvalRecorder:
    def __init__(self, repository: EvalRepository) -> None:
        self.repository = repository

    def record(
        self,
        tenant_id: str,
        eval_case_id: str,
        run_id: str,
        scores: EvalScores,
    ) -> Mapping[str, Any]:
        payload = scores.as_payload()
        result = self.repository.create_eval_result(
            tenant_id,
            eval_case_id,
            run_id,
            payload,
        )
        self.repository.append_event(
            tenant_id,
            run_id,
            "eval_result_created",
            {"eval_result_id": str(result["id"]), "scores": payload},
        )
        return result
