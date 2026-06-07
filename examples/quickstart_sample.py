#!/usr/bin/env python3
"""Run deterministic harness sample use cases."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rowplane.samples import run_sample_suite


def main() -> None:
    result = run_sample_suite()
    print(json.dumps(asdict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
