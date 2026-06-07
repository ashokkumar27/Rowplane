"""Rowplane compatibility facade for ``pg_agent.adapters.openai_agents``."""

from importlib import import_module as _import_module
import sys as _sys

_module = _import_module("pg_agent.adapters.openai_agents")
_sys.modules[__name__] = _module
