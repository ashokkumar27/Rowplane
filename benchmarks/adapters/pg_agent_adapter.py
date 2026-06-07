"""Backward-compatible import for the Rowplane benchmark adapter."""

from benchmarks.adapters.rowplane_adapter import RowplaneAdapter

PgAgentAdapter = RowplaneAdapter

__all__ = ["PgAgentAdapter", "RowplaneAdapter"]
