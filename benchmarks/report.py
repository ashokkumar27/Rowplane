"""Markdown report generation for benchmark results."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from benchmarks.framework_matrix import framework_positions
from benchmarks.scoring import aggregate_scores
from benchmarks.types import BenchmarkRunRecord


RESEARCH_SOURCES = [
    ("LangChain agents", "https://docs.langchain.com/oss/python/langchain/agents"),
    ("LangGraph durable execution", "https://docs.langchain.com/oss/python/langgraph/durable-execution"),
    ("CrewAI docs", "https://docs.crewai.com/"),
    ("Pydantic AI agents", "https://ai.pydantic.dev/agent/"),
    ("OpenAI Agents SDK", "https://openai.github.io/openai-agents-python/running_agents/"),
    ("LlamaIndex agents", "https://developers.llamaindex.ai/"),
    ("AgentBench", "https://arxiv.org/abs/2308.03688"),
    ("GAIA", "https://arxiv.org/abs/2311.12983"),
]


def write_report(records: Sequence[BenchmarkRunRecord], output_path: Path, *, model: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(records, model=model), encoding="utf-8")


def render_report(records: Sequence[BenchmarkRunRecord], *, model: str) -> str:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Postgres-Native Agent Harness Benchmark",
        "",
        f"Generated: `{generated_at}`",
        f"Model: `{model}`",
        "",
        "This benchmark measures whether a Postgres-native control plane adds practical value for governed, auditable agent work. It is not a generic agent-framework leaderboard.",
        "",
    ]
    if not records:
        lines.extend(
            [
                "## Status",
                "",
                "No live benchmark records have been generated yet.",
                "",
                "Run:",
                "",
                "```bash",
                "OPENAI_API_KEY=... DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rowplane \\",
                "  python -m benchmarks.run --model gpt-5.4-mini --repeats 3 --max-usd 5",
                "```",
                "",
            ]
        )
    else:
        lines.extend(_summary_section(records))
        lines.extend(_scenario_section(records))
        lines.extend(_interpretation_section(records))
    lines.extend(_framework_positioning_section())
    lines.extend(
        [
            "## Method",
            "",
            "- Default scored systems are `rowplane` and `plain_openai_tool_loop`.",
            "- `plain_openai_tool_loop` is the practical baseline: the same model and Python tools without a durable SQL control plane.",
            "- Other frameworks are included as a positioning matrix unless native adapters are implemented for them.",
            "- Scores weight functional correctness, governance, SQL/audit evidence, cost/latency, and developer effort.",
            "- Rowplane is judged on its intended niche: Postgres-native control plane, approvals, replay/search, tenant evidence, and durable traceability.",
            "",
            "## Sources",
            "",
        ]
    )
    lines.extend(f"- [{name}]({url})" for name, url in RESEARCH_SOURCES)
    lines.append("")
    return "\n".join(lines)


def _summary_section(records: Sequence[BenchmarkRunRecord]) -> list[str]:
    summary = aggregate_scores(records)
    lines = [
        "## Summary",
        "",
        "| System | Runs | Avg Score | Task | Control Plane | Ops | Pass Rate | Avg Latency | Est. Cost |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for framework, item in sorted(summary.items(), key=lambda pair: pair[1]["average_total"], reverse=True):
        latency = item["average_latency_ms"]
        lines.append(
            f"| {framework} | {item['runs']} | {item['average_total']} | "
            f"{item['average_task_success']} | {item['average_harness_control_plane']} | "
            f"{item['average_operational_efficiency']} | {item['pass_rate']:.3f} | "
            f"{latency if latency is not None else 'n/a'} | ${item['estimated_cost_usd']:.6f} |"
        )
    lines.append("")
    return lines


def _scenario_section(records: Sequence[BenchmarkRunRecord]) -> list[str]:
    grouped: dict[str, list[BenchmarkRunRecord]] = defaultdict(list)
    for record in records:
        grouped[record.scenario].append(record)
    lines = ["## Scenario Results", ""]
    for scenario, items in sorted(grouped.items()):
        lines.append(f"### {scenario}")
        lines.append("")
        lines.append("| System | Repeat | Score | Errors |")
        lines.append("| --- | ---: | ---: | --- |")
        for item in sorted(items, key=lambda record: (record.framework, record.repeat)):
            errors = "; ".join(item.errors) if item.errors else ""
            lines.append(f"| {item.framework} | {item.repeat} | {item.score.get('total', 0)} | {errors} |")
        lines.append("")
    return lines


def _interpretation_section(records: Sequence[BenchmarkRunRecord]) -> list[str]:
    rowplane = [record for record in records if record.framework == "rowplane"]
    baselines = [record for record in records if record.framework != "rowplane"]
    rowplane_avg = _avg(record.score.get("total", 0.0) for record in rowplane)
    baseline_avg = _avg(record.score.get("total", 0.0) for record in baselines)
    return [
        "## Interpretation",
        "",
        f"- Rowplane average: `{rowplane_avg:.2f}`.",
        f"- Non-Rowplane runnable baseline average: `{baseline_avg:.2f}`.",
        "- If Rowplane wins, the evidence should come from SQL-enforced governance, auditability, replay/search, tenant controls, and durable run state.",
        "- If Rowplane loses a scenario, treat it as a concrete product gap in prompt contract, API ergonomics, or harness behavior.",
        "- Do not present these numbers as a generic LangGraph/CrewAI/LangChain leaderboard unless native adapters are implemented for those frameworks.",
        "",
    ]


def _framework_positioning_section() -> list[str]:
    lines = [
        "## Framework Positioning",
        "",
        "Comparing with other agent frameworks is useful for positioning, not as a default scored leaderboard. Rowplane should be evaluated against its niche: SQL-native governance and auditability.",
        "",
        "| System | Primary Job | Strongest When | Postgres-Native Fit | Benchmark Role |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in framework_positions():
        lines.append(
            f"| {item.name} | {item.primary_job} | {item.strongest_when} | "
            f"{item.postgres_native_fit} | {item.benchmark_role} |"
        )
    lines.append("")
    return lines


def _avg(values: Sequence[object]) -> float:
    numeric = [float(value) for value in values]
    return sum(numeric) / len(numeric) if numeric else 0.0
