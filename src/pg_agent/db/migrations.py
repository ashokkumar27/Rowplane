"""Simple SQL migration runner."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pg_agent.db.repository import migration_files


DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "migrations"


def apply_migrations(conn: Any, migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR) -> list[str]:
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version text PRIMARY KEY,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        for path in migration_files(migrations_dir):
            version = path.name
            cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s", [version])
            if cur.fetchone() is not None:
                continue
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", [version])
            applied.append(version)
    return applied


def main() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    import psycopg

    with psycopg.connect(database_url, autocommit=False) as conn:
        applied = apply_migrations(conn)
        conn.commit()
    for version in applied:
        print(version)


if __name__ == "__main__":
    main()
