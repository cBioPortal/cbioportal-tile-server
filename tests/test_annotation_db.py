from __future__ import annotations

import pytest

from app.annotation_db import migrate


class FakeConnection:
    def __init__(self, version: int | None = None):
        self.version = version
        self.statements: list[str] = []

    async def execute(self, statement: str, *args):
        self.statements.append(statement)

    async def fetchval(self, statement: str, *args):
        return self.version


@pytest.mark.asyncio
async def test_annotation_migration_creates_schema_once():
    connection = FakeConnection()

    await migrate(connection)

    assert any("annotation_schema_migrations" in item for item in connection.statements)
    assert any("CREATE TABLE IF NOT EXISTS annotations" in item for item in connection.statements)
    assert any("INSERT INTO annotation_schema_migrations" in item for item in connection.statements)


@pytest.mark.asyncio
async def test_annotation_migration_skips_completed_version():
    connection = FakeConnection(version=1)

    await migrate(connection)

    assert len(connection.statements) == 1
    assert "annotation_schema_migrations" in connection.statements[0]
