"""Migration runner — mirrors packages/db/src/index.ts migrateDatabase()."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import asyncpg

_MIGRATION_LOCK = "node-agent-platform:migrations"


async def migrate_database(pool: asyncpg.Pool, migrations_dir: str | None = None) -> None:
    candidates = [
        Path(migrations_dir) if migrations_dir else None,
        Path(__file__).resolve().parent.parent.parent / "migrations",
        Path.cwd() / "packages" / "db" / "migrations",
        Path.cwd().parent.parent / "packages" / "db" / "migrations",
    ]
    migration_directory: Path | None = None
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            migration_directory = candidate
            break
    if not migration_directory:
        raise FileNotFoundError("Database migration directory was not found.")

    conn: asyncpg.Connection = await pool.acquire()
    try:
        await conn.execute("SELECT pg_advisory_lock(hashtext($1))", _MIGRATION_LOCK)
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                 id text PRIMARY KEY,
                 applied_at timestamptz NOT NULL DEFAULT now()
               )"""
        )
        files = sorted(
            f
            for f in os.listdir(migration_directory)
            if re.match(r"^\d+.*\.sql$", f)
        )
        for file in files:
            applied = await conn.fetchrow(
                "SELECT 1 FROM schema_migrations WHERE id = $1", file
            )
            if applied:
                continue
            migration_sql = (migration_directory / file).read_text(encoding="utf-8")
            try:
                await conn.execute("BEGIN")
                await conn.execute(migration_sql)
                await conn.execute("INSERT INTO schema_migrations (id) VALUES ($1)", file)
                await conn.execute("COMMIT")
            except Exception:
                await conn.execute("ROLLBACK")
                raise
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock(hashtext($1))", _MIGRATION_LOCK)
        finally:
            await pool.release(conn)


async def main() -> None:
    database_url = os.environ.get(
        "DATABASE_URL", "postgresql://agent:agent@127.0.0.1:55432/agent"
    )
    pool = await asyncpg.create_pool(database_url)
    try:
        await migrate_database(pool)
        print("Database migration completed.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
