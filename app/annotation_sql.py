"""One transaction against annotation storage, SQLite or Postgres alike.

The comment, slide-note and annotation batch routes write their SQL once,
in Postgres form (``$1`` placeholders), and run it through ``transaction``:

- ``$n`` placeholders become SQLite's numbered ``?n``;
- ``tx.now`` is the current UTC time and ``tx.ts(column)`` selects a
  timestamp column as ``YYYY-MM-DDTHH:MM:SSZ`` (SQLite stores that text
  already, Postgres formats its ``TIMESTAMPTZ``);
- ``tx.for_update`` locks the rows a write transaction read (Postgres);
  a SQLite write transaction starts with ``BEGIN IMMEDIATE`` and so holds
  the database's write lock from its first statement.

Any exception rolls the whole transaction back.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import aiosqlite

from .annotation_db import connection

_PLACEHOLDER = re.compile(r"\$(\d+)")
_PG_TIMESTAMP = "YYYY-MM-DD\"T\"HH24:MI:SS\"Z\""


class Transaction:
    """The open transaction's query methods and dialect fragments."""

    def __init__(self, kind: str, handle: Any):
        self.kind = kind
        self._handle = handle
        if kind == "postgres":
            self.now = "now()"
            self.for_update = " FOR UPDATE"
        else:
            self.now = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
            self.for_update = ""

    def ts(self, column: str, alias: str | None = None) -> str:
        """``column`` as UTC ISO-8601 text, named ``alias`` (or its own name)."""
        name = alias or column.rsplit(".", 1)[-1]
        if self.kind == "postgres":
            return f"to_char({column} AT TIME ZONE 'UTC', '{_PG_TIMESTAMP}') AS {name}"
        return f"{column} AS {name}"

    def created_order(self, alias: str) -> str:
        """ORDER BY terms listing ``alias``'s rows oldest first.

        SQLite stores whole seconds, so it orders by insertion (``rowid``);
        Postgres keeps microseconds.
        """
        if self.kind == "postgres":
            return f"{alias}.created_at, {alias}.id"
        return f"{alias}.rowid"

    def _sql(self, statement: str) -> str:
        if self.kind == "postgres":
            return statement
        return _PLACEHOLDER.sub(r"?\1", statement)

    async def fetch(self, statement: str, *args: Any) -> list[dict]:
        if self.kind == "postgres":
            return [dict(row) for row in await self._handle.fetch(statement, *args)]
        cursor = await self._handle.execute(self._sql(statement), args)
        return [dict(row) for row in await cursor.fetchall()]

    async def fetchrow(self, statement: str, *args: Any) -> dict | None:
        rows = await self.fetch(statement, *args)
        return rows[0] if rows else None

    async def execute(self, statement: str, *args: Any) -> int:
        """Run a write; returns the number of rows it changed."""
        if self.kind == "postgres":
            status = await self._handle.execute(statement, *args)
            tail = status.rsplit(" ", 1)[-1]
            return int(tail) if tail.isdigit() else 0
        cursor = await self._handle.execute(self._sql(statement), args)
        return cursor.rowcount


@asynccontextmanager
async def transaction(
    kind: str,
    *,
    db_path: Callable[[], str],
    db_url: Callable[[], str],
    pragmas: Callable[[aiosqlite.Connection], Any],
    write: bool = False,
) -> AsyncIterator[Transaction]:
    """Open one transaction on the configured storage (see the module notes)."""
    if kind == "postgres":
        async with connection(db_url()) as conn:
            async with conn.transaction():
                yield Transaction("postgres", conn)
        return

    async with aiosqlite.connect(db_path()) as db:
        await pragmas(db)
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield Transaction("sqlite", db)
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
