"""Memory persistence and filtered retrieval."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class MemorySearch:
    tenant_id: str
    memory_type: str | None = None
    metadata_contains: dict[str, Any] = field(default_factory=dict)
    source_run_id: str | None = None
    query: str | None = None
    embedding: Sequence[float] | None = None
    limit: int = 10

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id is required for memory search")
        if self.limit < 1 or self.limit > 100:
            raise ValueError("memory search limit must be between 1 and 100")


def build_memory_where(search: MemorySearch) -> tuple[str, list[Any]]:
    clauses = ["tenant_id = %s"]
    params: list[Any] = [search.tenant_id]
    if search.memory_type is not None:
        clauses.append("memory_type = %s")
        params.append(search.memory_type)
    if search.metadata_contains:
        clauses.append("metadata @> %s::jsonb")
        params.append(search.metadata_contains)
    if search.source_run_id is not None:
        clauses.append("source_run_id = %s")
        params.append(search.source_run_id)
    if search.query:
        clauses.append("to_tsvector('simple', memory_type || ' ' || content || ' ' || metadata::text) @@ websearch_to_tsquery('simple', %s)")
        params.append(search.query)
    return " AND ".join(clauses), params


def vector_literal(embedding: Sequence[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in embedding) + "]"


class MemoryRepository(Protocol):
    def create_memory(
        self,
        tenant_id: str,
        memory_type: str,
        content: str,
        metadata: Mapping[str, Any],
        *,
        source_run_id: str | None = None,
        embedding: Sequence[float] | None = None,
    ) -> Mapping[str, Any]: ...

    def search_memory(self, search: MemorySearch) -> list[Mapping[str, Any]]: ...
