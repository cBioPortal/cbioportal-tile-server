from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.annotation_db import SCHEMA_LOCK_KEY, migrate, migrate_agent


class FakeConnection:
    def __init__(self, version: int | None = None):
        self.version = version
        self.statements: list[str] = []
        self.events: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("begin")
        yield
        self.events.append("commit")

    async def execute(self, statement: str, *args):
        self.statements.append(statement)
        self.events.append(statement)
        if "pg_advisory_xact_lock" in statement:
            assert args == (SCHEMA_LOCK_KEY,)

    async def fetchval(self, statement: str, *args):
        return self.version


def _lock_taken_first(connection: FakeConnection) -> bool:
    return (
        connection.events[0] == "begin"
        and "pg_advisory_xact_lock" in connection.events[1]
        and connection.events[-1] == "commit"
    )


@pytest.mark.asyncio
async def test_annotation_migration_creates_schema_once():
    connection = FakeConnection()

    await migrate(connection)

    assert _lock_taken_first(connection)
    assert any("annotation_schema_migrations" in item for item in connection.statements)
    assert any("CREATE TABLE IF NOT EXISTS annotations" in item for item in connection.statements)
    assert any("INSERT INTO annotation_schema_migrations" in item for item in connection.statements)


@pytest.mark.asyncio
async def test_annotation_migration_skips_completed_version():
    connection = FakeConnection(version=1)

    await migrate(connection)

    assert _lock_taken_first(connection)
    assert len(connection.statements) == 2
    assert "annotation_schema_migrations" in connection.statements[1]


@pytest.mark.asyncio
async def test_agent_migration_holds_schema_lock():
    connection = FakeConnection()

    await migrate_agent(connection)

    assert _lock_taken_first(connection)
    assert sum("pg_advisory_xact_lock" in item for item in connection.statements) == 1
    assert any("CREATE TABLE IF NOT EXISTS agent_actions" in item for item in connection.statements)
