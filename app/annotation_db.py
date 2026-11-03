"""Shared annotation database lifecycle and versioned schema migrations."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

_pool: asyncpg.Pool | None = None
_pool_dsn = ""


def _pool_size(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


async def open_pool(dsn: str) -> asyncpg.Pool:
    """Open one process-local pool for the shared Postgres/Lakebase database."""
    global _pool, _pool_dsn
    if _pool is not None and _pool_dsn == dsn:
        return _pool
    await close_pool()
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=_pool_size("ANNOTATION_DB_POOL_MIN_SIZE", 1),
        max_size=_pool_size("ANNOTATION_DB_POOL_MAX_SIZE", 8),
        command_timeout=float(
            os.environ.get("ANNOTATION_DB_COMMAND_TIMEOUT", "30")
        ),
    )
    _pool_dsn = dsn
    return _pool


async def close_pool() -> None:
    global _pool, _pool_dsn
    if _pool is not None:
        await _pool.close()
    _pool = None
    _pool_dsn = ""


@asynccontextmanager
async def connection(dsn: str) -> AsyncIterator[asyncpg.Connection]:
    pool = await open_pool(dsn)
    async with pool.acquire() as conn:
        yield conn


async def migrate(conn: asyncpg.Connection) -> None:
    """Apply idempotent schema version 1 without destructive table rewrites."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS annotation_schema_migrations (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    current = await conn.fetchval(
        "SELECT version FROM annotation_schema_migrations WHERE component = 'annotations'"
    )
    if current is not None and int(current) >= 1:
        return

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS annotations (
            id          TEXT PRIMARY KEY,
            slide_id    TEXT NOT NULL,
            study_id    TEXT NOT NULL,
            body        TEXT NOT NULL,
            target      TEXT NOT NULL,
            created_by  TEXT NOT NULL,
            visible_to  TEXT,
            version     INTEGER NOT NULL DEFAULT 1,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ann_slide_study ON annotations(slide_id, study_id)"
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_ann_slide ON annotations(slide_id)")
    await conn.execute(
        """
        INSERT INTO annotation_schema_migrations(component, version)
        VALUES ('annotations', 1)
        ON CONFLICT (component) DO UPDATE SET version = EXCLUDED.version,
                                               applied_at = timezone('utc', now())
        """
    )
