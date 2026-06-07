"""Rowplane compatibility facade for ``pg_agent.memory``."""

from importlib import import_module as _import_module

_impl = _import_module("pg_agent.memory")

for _name in getattr(_impl, "__all__", ()):  # re-export documented package symbols
    globals()[_name] = getattr(_impl, _name)

__all__ = list(getattr(_impl, "__all__", ()))

def __getattr__(name: str):
    return getattr(_impl, name)
