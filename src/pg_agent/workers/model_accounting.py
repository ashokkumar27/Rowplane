"""Model-call accounting helpers for deterministic workers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def model_usage(model_client: Any) -> Mapping[str, Any]:
    """Return sanitized usage metadata exposed by simple model clients."""
    for attr in ("last_usage", "usage", "last_model_call"):
        value = getattr(model_client, attr, None)
        if isinstance(value, Mapping):
            return value
    getter = getattr(model_client, "get_last_usage", None)
    if callable(getter):
        value = getter()
        if isinstance(value, Mapping):
            return value
    return {}


def projected_model_cost(model_client: Any) -> float:
    for attr in ("estimated_call_cost_usd", "projected_call_cost_usd"):
        value = getattr(model_client, attr, None)
        if value is not None:
            return _float_or_zero(value)
    usage = model_usage(model_client)
    for key in ("estimated_cost_usd", "cost_usd"):
        if key in usage:
            return _float_or_zero(usage[key])
    return 0.0


def complete_model_call(
    repository: Any,
    tenant_id: str,
    run_id: str,
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
    model: str = "unset",
    status: str,
    latency_ms: int,
    model_client: Any,
    error: str | None = None,
) -> None:
    if not hasattr(repository, "complete_model_call"):
        return
    usage = model_usage(model_client)
    repository.complete_model_call(
        tenant_id,
        run_id,
        task_id=task_id,
        agent_id=agent_id,
        model=model,
        status=status,
        latency_ms=latency_ms,
        prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
        completion_tokens=_int_or_none(usage.get("completion_tokens")),
        total_tokens=_int_or_none(usage.get("total_tokens")),
        estimated_cost_usd=_float_or_none(
            usage.get("estimated_cost_usd", usage.get("cost_usd"))
        ),
        error=error,
        actor="worker",
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: Any) -> float:
    converted = _float_or_none(value)
    return 0.0 if converted is None else converted
