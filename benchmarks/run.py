"""Run the live usefulness benchmark."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from benchmarks.adapters import build_adapters
from benchmarks.report import write_report
from benchmarks.scenarios import build_scenarios
from benchmarks.types import BenchmarkRunRecord

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "benchmarks" / "results" / "latest.json"
DEFAULT_REPORT = ROOT / "benchmarks" / "reports" / "usefulness_benchmark.md"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-usd", type=float, default=25.0)
    parser.add_argument("--framework", action="append", help="Limit to one or more runnable system names.")
    parser.add_argument("--scenario", action="append", help="Limit to one or more scenario names.")
    parser.add_argument(
        "--include-experimental-frameworks",
        action="store_true",
        help="Also run non-native framework smoke wrappers. Do not use these as leaderboard results.",
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--list", action="store_true", help="List frameworks and scenarios without running.")
    args = parser.parse_args()

    scenarios = build_scenarios()
    adapters = build_adapters(
        database_url=args.database_url,
        include_experimental_frameworks=args.include_experimental_frameworks,
    )

    if args.list:
        print(json.dumps({
            "runnable_systems": [adapter.name for adapter in adapters],
            "scenarios": [scenario.name for scenario in scenarios],
            "comparison_note": "Default benchmark is rowplane vs plain_openai_tool_loop. Other frameworks are positioning context unless --include-experimental-frameworks is used.",
        }, indent=2))
        return

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required for live benchmark runs")

    if args.framework:
        wanted = set(args.framework)
        adapters = [adapter for adapter in adapters if adapter.name in wanted]
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [scenario for scenario in scenarios if scenario.name in wanted]

    records: list[BenchmarkRunRecord] = []
    spent = 0.0
    for repeat in range(1, args.repeats + 1):
        for scenario in scenarios:
            for adapter in adapters:
                if spent >= args.max_usd:
                    raise SystemExit(f"Cost guard reached before next run: ${spent:.6f}")
                record = adapter.run(scenario, repeat=repeat, model=args.model)
                records.append(record)
                spent += record.estimated_cost_usd or 0.0
                _write_results(args.results, records, args.model)
                write_report(records, args.report, model=args.model)
                print(
                    json.dumps(
                        {
                            "framework": record.framework,
                            "scenario": record.scenario,
                            "repeat": record.repeat,
                            "score": record.score,
                            "errors": record.errors,
                            "spent_usd": round(spent, 6),
                        },
                        sort_keys=True,
                    )
                )


def _write_results(path: Path, records: list[BenchmarkRunRecord], model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "benchmark_kind": "postgres_native_control_plane",
        "model": model,
        "records": [record.to_dict() for record in records],
        "comparison_note": "Default results compare rowplane with a plain OpenAI tool-loop baseline. Experimental framework wrappers are not native framework implementations.",
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
