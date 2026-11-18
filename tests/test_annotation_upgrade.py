"""Upgrading storage written by the previous schema keeps every annotation."""

import json
import os
import uuid

import aiosqlite
import pytest

from app import annotation_comments, annotation_db, annotations, slide_notes
from app.annotation_comments import CommentIn
from app.slide_notes import SlideNoteIn

SLIDE_KEY = "0123456789ab"
ROWS = 250

# The SQLite schema before annotation_comments and slide_notes.
PREVIOUS_SQLITE_SCHEMA = """
CREATE TABLE annotations (
    id          TEXT PRIMARY KEY,
    slide_id    TEXT NOT NULL,
    study_id    TEXT NOT NULL,
    body        TEXT NOT NULL,
    target      TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    visible_to  TEXT,
    version     INTEGER DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_ann_slide_study ON annotations(slide_id, study_id);
CREATE INDEX idx_ann_slide ON annotations(slide_id);
"""

# The Postgres schema at version 1 of the "annotations" component.
PREVIOUS_POSTGRES_SCHEMA = """
CREATE TABLE annotation_schema_migrations (
    component TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
);
CREATE TABLE annotations (
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
);
CREATE INDEX idx_ann_slide_study ON annotations(slide_id, study_id);
CREATE INDEX idx_ann_slide ON annotations(slide_id);
INSERT INTO annotation_schema_migrations(component, version) VALUES ('annotations', 1);
"""


def _previous_rows() -> list[tuple]:
    return [
        (
            f"legacy-{index}",
            SLIDE_KEY,
            "study-1",
            json.dumps({"label": f"L{index}", "comment": f"note {index}", "type": "Polygon"}),
            json.dumps({"selector": {"type": "SvgSelector", "value": "<svg/>"}}),
            "local-development",
            "[]" if index % 2 else None,
            1 + index % 3,
        )
        for index in range(ROWS)
    ]


async def _check_upgraded() -> None:
    owner = {"sub": "local-development", "groups": [], "study_id": "study-1"}
    listed = await annotations.list_annotations(SLIDE_KEY, owner, include_rejected=True)
    assert len(listed) == ROWS
    by_id = {row.id: row for row in listed}
    for index in (0, 1, ROWS - 1):
        row = by_id[f"legacy-{index}"]
        assert row.body.label == f"L{index}"
        assert row.body.comment == f"note {index}"
        assert row.version == 1 + index % 3
        assert row.visible_to == ([] if index % 2 else None)
        assert row.comment_count == 0

    reader = {"sub": "reader", "groups": [], "study_id": "study-1"}
    await annotation_comments.create_comment("legacy-1", CommentIn(body="on an old row"), reader)
    note = await slide_notes.create_slide_note(
        SlideNoteIn(slide_id=SLIDE_KEY, study_id="study-1", tags=["QC"], visible_to=[]), reader
    )
    assert note.version == 1
    listed = await annotations.list_annotations(SLIDE_KEY, reader)
    assert len(listed) == ROWS // 2
    assert {row.id: row.comment_count for row in listed}["legacy-1"] == 1


@pytest.mark.asyncio
async def test_sqlite_upgrade_keeps_existing_annotations(tmp_path, monkeypatch):
    monkeypatch.setattr(annotations.settings, "annotation_database_url", "")
    path = str(tmp_path / "annotations.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript(PREVIOUS_SQLITE_SCHEMA)
        await db.executemany(
            "INSERT INTO annotations"
            " (id, slide_id, study_id, body, target, created_by, visible_to, version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            _previous_rows(),
        )
        await db.commit()

    try:
        await annotations.init_db(path, db_url="")
        await annotations.init_db(path, db_url="")  # a second start changes nothing
        await _check_upgraded()
    finally:
        annotations._db_path = ""
        annotations._db_url = ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_upgrade_from_version_one_keeps_existing_annotations(monkeypatch):
    import asyncpg

    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.fail("POSTGRES_TEST_DSN is required for the PostgreSQL integration suite")
    schema = f"upgrade_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setattr(annotation_db, "_pool", None)
    monkeypatch.setattr(annotation_db, "_pool_dsn", "")
    try:
        legacy = await asyncpg.connect(dsn, server_settings={"search_path": schema})
        try:
            await legacy.execute(PREVIOUS_POSTGRES_SCHEMA)
            await legacy.executemany(
                "INSERT INTO annotations"
                " (id, slide_id, study_id, body, target, created_by, visible_to, version)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                _previous_rows(),
            )
        finally:
            await legacy.close()

        separator = "&" if "?" in dsn else "?"
        scoped = f"{dsn}{separator}search_path={schema}"
        await annotations.init_db(db_url=scoped)
        await annotations.init_db(db_url=scoped)
        await _check_upgraded()

        versions = await admin.fetch(
            f'SELECT component, version FROM "{schema}".annotation_schema_migrations'
        )
        assert {row["component"]: row["version"] for row in versions} == {"annotations": 2}
        assert await admin.fetchval(f'SELECT count(*) FROM "{schema}".annotations') == ROWS
    finally:
        await annotations.close_db()
        annotations._db_path = ""
        annotations._db_url = ""
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
