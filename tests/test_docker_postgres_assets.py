from __future__ import annotations

from pathlib import Path
import unittest

from helpers import ROOT, SRC  # noqa: F401


class DockerPostgresAssetsTests(unittest.TestCase):
    def test_kiss_docs_model_covers_developer_and_postgres_paths(self) -> None:
        docs = {path.name for path in (ROOT / "docs").glob("*.md")}
        self.assertEqual(docs, {"TUTORIAL.md", "REFERENCE.md", "SCALING.md"})

        tutorial = (ROOT / "docs" / "TUTORIAL.md").read_text(encoding="utf-8")
        reference = (ROOT / "docs" / "REFERENCE.md").read_text(encoding="utf-8")

        for expected in (
            "AgentHarness",
            "@tool",
            "approval_requests",
            "tool_executions",
            "agent_events",
            "app.run_trajectory_v",
            "rowplane",
            "explain",
        ):
            self.assertIn(expected, tutorial)

        for expected in (
            "Core Tables",
            "Python API",
            "CLI",
            "SQL Runtime",
            "Management API",
            "Docker And Examples",
        ):
            self.assertIn(expected, reference)

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        starter = (ROOT / "examples" / "starters" / "refund_agent" / "README.md").read_text(
            encoding="utf-8"
        )
        support_starter = (ROOT / "examples" / "starters" / "customer_support_agent" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("docs/TUTORIAL.md", readme)
        self.assertIn("docs/REFERENCE.md", readme)
        self.assertIn("docs/SCALING.md", readme)
        self.assertIn("docs/TUTORIAL.md", starter)
        self.assertIn("docs/REFERENCE.md", starter)
        adapter_example = (ROOT / "examples" / "adapters" / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("drain_leased_work", support_starter)
        self.assertIn("approval", support_starter)
        self.assertIn("--live", support_starter)
        self.assertIn("--max-output-tokens 2400", support_starter)
        self.assertIn("OpenAI Agents", adapter_example)
        self.assertIn("command", adapter_example)

    def test_compose_defines_postgres_and_real_use_case_runner(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("postgres:", compose)
        self.assertIn("postgres-use-cases:", compose)
        self.assertIn("DATABASE_URL", compose)
        self.assertIn("examples/postgres_showcase.py", compose)
        self.assertIn("condition: service_healthy", compose)

    def test_postgres_image_layers_required_extensions(self) -> None:
        dockerfile = (ROOT / "docker" / "postgres" / "Dockerfile").read_text(
            encoding="utf-8"
        )

        self.assertIn("ghcr.io/pgmq/pg17-pgmq", dockerfile)
        self.assertIn("postgresql-17-cron", dockerfile)
        self.assertIn("postgresql-17-pgvector", dockerfile)
        self.assertIn("shared_preload_libraries", dockerfile)


if __name__ == "__main__":
    unittest.main()
