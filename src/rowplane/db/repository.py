"""Rowplane compatibility facade for ``pg_agent.db.repository``."""

from importlib import import_module as _import_module
import sys as _sys

_module = _import_module("pg_agent.db.repository")
_sys.modules[__name__] = _module
