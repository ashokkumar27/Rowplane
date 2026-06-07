from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "pg_agent" / "management" / "static"


class ManagementWebAssetTests(unittest.TestCase):
    def test_console_static_assets_exist_and_reference_api_surface(self) -> None:
        index = (STATIC / "index.html").read_text()
        script = (STATIC / "app.js").read_text()
        styles = (STATIC / "styles.css").read_text()

        self.assertIn("Rowplane Console", index)
        self.assertIn("Governed agent operations", index)
        self.assertIn("connectionState", index)
        self.assertIn("attentionStrip", index)
        self.assertIn('/console/assets/styles.css', index)
        self.assertIn('/console/assets/app.js', index)
        for endpoint in (
            "/api/metrics/overview",
            "/api/approvals",
            "/api/runs",
            "/api/tools",
            "/api/agents",
            "/api/evals",
            "/api/audit/events",
            "/api/memory",
        ):
            self.assertIn(endpoint, script)
        self.assertIn(".metric-grid", styles)
        self.assertIn(".attention-strip", styles)
        self.assertIn(".selected-row", styles)
        self.assertIn(".detail-panel", styles)
        self.assertIn("setConnection", script)
        self.assertIn("rawDetails", script)

    def test_docker_compose_exposes_management_api(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertIn("management-api:", compose)
        self.assertIn('"8000:8000"', compose)
        self.assertIn("rowplane.management.api:app", compose)


if __name__ == "__main__":
    unittest.main()
