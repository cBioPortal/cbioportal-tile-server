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
        command_timeout=float(os.environ.get("ANNOTATION_DB_COMMAND_TIMEOUT", "30")),
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


async def _ensure_migration_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS annotation_schema_migrations (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
        )
        """
    )


async def _migration_complete(
    conn: asyncpg.Connection, component: str
) -> bool:
    current = await conn.fetchval(
        "SELECT version FROM annotation_schema_migrations WHERE component = $1",
        component,
    )
    return current is not None and int(current) >= 1


async def _mark_migration(conn: asyncpg.Connection, component: str) -> None:
    await conn.execute(
        """
        INSERT INTO annotation_schema_migrations(component, version)
        VALUES ($1, 1)
        ON CONFLICT (component) DO UPDATE SET version = EXCLUDED.version,
                                               applied_at = timezone('utc', now())
        """,
        component,
    )


async def migrate(conn: asyncpg.Connection) -> None:
    """Apply the annotation schema without destructive table rewrites."""
    await _ensure_migration_table(conn)
    if await _migration_complete(conn, "annotations"):
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
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ann_slide ON annotations(slide_id)"
    )
    await _mark_migration(conn, "annotations")


async def migrate_agent(conn: asyncpg.Connection) -> None:
    """Create the durable proposal audit table in the same database."""
    await _ensure_migration_table(conn)
    if not await _migration_complete(conn, "agent_actions"):
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_actions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_sub TEXT NOT NULL,
                study_id TEXT NOT NULL,
                slide_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
                decided_at TIMESTAMPTZ,
                outcome_json TEXT
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_actions_session "
            "ON agent_actions(session_id, user_sub)"
        )
        await _mark_migration(conn, "agent_actions")

    if not await _migration_complete(conn, "agent_retrieval_candidates"):
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_retrieval_candidates (
                session_id TEXT NOT NULL,
                user_sub TEXT NOT NULL,
                study_id TEXT NOT NULL,
                slide_id TEXT NOT NULL,
                candidate_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
                PRIMARY KEY (session_id, user_sub, study_id, slide_id, candidate_id)
            )
            """
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_retrieval_candidates_session "
            "ON agent_retrieval_candidates(session_id, user_sub, study_id, slide_id)"
        )
        await _mark_migration(conn, "agent_retrieval_candidates")

    if await _migration_complete(conn, "agent_retrieval_runs"):
        return

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_retrieval_runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            user_sub TEXT NOT NULL,
            study_id TEXT NOT NULL,
            slide_id TEXT NOT NULL,
            source_fingerprint TEXT,
            viewer_generation INTEGER,
            model TEXT NOT NULL,
            query TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now()),
            expires_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_retrieval_runs_scope "
        "ON agent_retrieval_runs(session_id, user_sub, study_id, slide_id, created_at)"
    )
    await _mark_migration(conn, "agent_retrieval_runs")
