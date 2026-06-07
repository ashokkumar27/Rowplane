"""Rowplane CLI module facade."""

import sys

from pg_agent.cli import *  # noqa: F403
from pg_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
