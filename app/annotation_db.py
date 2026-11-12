"""Shared annotation database lifecycle and versioned schema migrations.

The Postgres/Lakebase pool takes its password from the DSN, or, with
a password provider (``ANNOTATION_DATABASE_PASSWORD_PROVIDER``), from a short-lived
Lakebase credential fetched for each new connection
(``app.lakebase_credentials``).  Idle connections close after
``ANNOTATION_DB_MAX_INACTIVE_CONNECTION_LIFETIME`` seconds and every
connection is replaced once it is ``ANNOTATION_DB_MAX_CONNECTION_AGE_SECONDS``
old, so none outlives the credential it logged in with by much.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg

from . import lakebase_credentials

_pool: asyncpg.Pool | None = None
_pool_dsn = ""
_pool_generation_started = 0.0


def _pool_size(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _seconds(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def max_connection_age() -> float:
    """Seconds after which every pooled connection is replaced (0 = never)."""
    return _seconds("ANNOTATION_DB_MAX_CONNECTION_AGE_SECONDS", 1800)


async def open_pool(dsn: str) -> asyncpg.Pool:
    """Open one process-local pool for the shared Postgres/Lakebase database."""
    global _pool, _pool_dsn, _pool_generation_started
    if _pool is not None and _pool_dsn == dsn:
        return _pool
    await close_pool()
    _pool = await asyncpg.create_pool(
        dsn,
        password=lakebase_credentials.password_option(),
        min_size=_pool_size("ANNOTATION_DB_POOL_MIN_SIZE", 1),
        max_size=_pool_size("ANNOTATION_DB_POOL_MAX_SIZE", 8),
        max_inactive_connection_lifetime=_seconds(
            "ANNOTATION_DB_MAX_INACTIVE_CONNECTION_LIFETIME", 300
        ),
        command_timeout=float(
            os.environ.get("ANNOTATION_DB_COMMAND_TIMEOUT", "30")
        ),
    )
    _pool_dsn = dsn
    _pool_generation_started = time.monotonic()
    return _pool


async def _expire_aged_connections(pool: asyncpg.Pool) -> None:
    """Replace every pooled connection once the current set is too old."""
    global _pool_generation_started
    age = max_connection_age()
    now = time.monotonic()
    if age and now - _pool_generation_started >= age:
        _pool_generation_started = now
        await pool.expire_connections()


async def close_pool() -> None:
    global _pool, _pool_dsn
    if _pool is not None:
        await _pool.close()
    _pool = None
    _pool_dsn = ""


@asynccontextmanager
async def connection(dsn: str) -> AsyncIterator[asyncpg.Connection]:
    pool = await open_pool(dsn)
    await _expire_aged_connections(pool)
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
