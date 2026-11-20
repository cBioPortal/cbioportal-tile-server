from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.annotation_db import (
    ANNOTATIONS_SCHEMA_VERSION,
    SCHEMA_LOCK_KEY,
    migrate,
    migrate_agent,
)


class FakeConnection:
    def __init__(self, version: int | None = None):
        self.version = version
        self.statements: list[str] = []
        self.events: list[str] = []
        self.marked: list[tuple] = []

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
        if "INSERT INTO annotation_schema_migrations" in statement:
            self.marked.append(args)

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


def _created(connection: FakeConnection, table: str) -> bool:
    return any(f"CREATE TABLE IF NOT EXISTS {table}" in item for item in connection.statements)


@pytest.mark.asyncio
async def test_annotation_migration_skips_completed_version():
    connection = FakeConnection(version=ANNOTATIONS_SCHEMA_VERSION)

    await migrate(connection)

    assert _lock_taken_first(connection)
    assert len(connection.statements) == 2
    assert "annotation_schema_migrations" in connection.statements[1]


@pytest.mark.asyncio
async def test_fresh_annotation_schema_applies_every_version():
    connection = FakeConnection()

    await migrate(connection)

    assert ANNOTATIONS_SCHEMA_VERSION == 3
    for table in ("annotations", "annotation_comments", "slide_notes"):
        assert _created(connection, table)
    assert _adds_deleted_at(connection)
    assert connection.marked == [("annotations", 1), ("annotations", 2), ("annotations", 3)]


def _adds_deleted_at(connection: FakeConnection) -> bool:
    return any(
        "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ" in item
        for item in connection.statements
    )


def _only_additive(connection: FakeConnection) -> None:
    """Nothing rewrites or drops what an earlier version stored.

    The one ``ALTER TABLE`` adds the nullable ``deleted_at`` column (no
    default, so no row is rewritten).
    """
    for statement in connection.statements:
        upper = statement.upper()
        assert "DROP " not in upper
        assert "DELETE " not in upper and "UPDATE ANNOTATIONS" not in upper
        if "ALTER TABLE" in upper:
            assert "ADD COLUMN IF NOT EXISTS DELETED_AT TIMESTAMPTZ" in upper
            assert "DEFAULT" not in upper and "NOT NULL" not in upper


@pytest.mark.asyncio
async def test_version_one_schema_upgrades_additively():
    connection = FakeConnection(version=1)

    await migrate(connection)

    assert _lock_taken_first(connection)
    assert not _created(connection, "annotations")
    assert _created(connection, "annotation_comments")
    assert _created(connection, "slide_notes")
    assert _adds_deleted_at(connection)
    assert connection.marked == [("annotations", 2), ("annotations", 3)]
    _only_additive(connection)


@pytest.mark.asyncio
async def test_version_two_schema_adds_only_the_soft_delete_column():
    connection = FakeConnection(version=2)

    await migrate(connection)

    assert _lock_taken_first(connection)
    for table in ("annotations", "annotation_comments", "slide_notes"):
        assert not _created(connection, table)
    assert _adds_deleted_at(connection)
    assert any("idx_ann_deleted_at" in item for item in connection.statements)
    assert connection.marked == [("annotations", 3)]
    _only_additive(connection)


@pytest.mark.asyncio
async def test_agent_migration_holds_schema_lock():
    connection = FakeConnection()

    await migrate_agent(connection)

    assert _lock_taken_first(connection)
    assert sum("pg_advisory_xact_lock" in item for item in connection.statements) == 1
    assert any("CREATE TABLE IF NOT EXISTS agent_actions" in item for item in connection.statements)
