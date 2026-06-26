"""
Annotation CRUD with pluggable SQLite or Postgres storage.

Routes:
  GET    /annotations             list annotations for a slide/study
  POST   /annotations             create a new annotation
  PUT    /annotations/{id}        update (optimistic concurrency via version)
  DELETE /annotations/{id}        delete (creator only)

Auth: all routes require a valid Keycloak JWT via ``require_user()``.
Study-level ACL: annotations inherit study access from cBioPortal —
if a user can see the study they can read/write its annotations.
"""

import json
import logging
import uuid
from typing import Any

import aiosqlite
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .auth import require_user
from .config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotations", tags=["annotations"])

_db_path: str = ""
_db_url: str = ""


def _settings_db_url() -> str:
    return getattr(settings, "annotation_database_url", "")


def _storage_kind() -> str:
    return "postgres" if (_db_url or _settings_db_url()) else "sqlite"


def _get_db_path() -> str:
    return _db_path or settings.annotation_db_path


def _get_db_url() -> str:
    return _db_url or _settings_db_url()


async def _apply_sqlite_pragmas(db: aiosqlite.Connection) -> None:
    """Performance PRAGMAs applied to every SQLite connection."""
    await db.execute("PRAGMA synchronous  = NORMAL")
    await db.execute("PRAGMA cache_size   = -64000")
    await db.execute("PRAGMA temp_store   = MEMORY")
    await db.execute("PRAGMA mmap_size    = 268435456")
    await db.execute("PRAGMA busy_timeout = 5000")


def _sqlite_target_json(selector: dict[str, Any]) -> str:
    return json.dumps({"selector": selector})


def _decode_visible_to(value: str | None) -> list[str] | None:
    return json.loads(value) if value is not None else None


def _is_visible(row: dict, user_sub: str, user_groups: set[str]) -> bool:
    if row["created_by"] == user_sub:
        return True
    visible_to = row["visible_to"]
    if visible_to is None:
        return False
    groups = json.loads(visible_to) if isinstance(visible_to, str) else visible_to
    if not groups:
        return True
    return bool(user_groups.intersection(groups))


class AnnotationBody(BaseModel):
    label: str = ""
    comment: str = ""
    type: str = ""


class AnnotationTarget(BaseModel):
    selector: dict[str, Any]


class AnnotationIn(BaseModel):
    slide_id: str
    study_id: str
    body: AnnotationBody
    target: AnnotationTarget
    visible_to: list[str] | None = Field(
        default=None,
        description="Keycloak group names that may view this annotation; null = creator only",
    )


class AnnotationOut(BaseModel):
    id: str
    slide_id: str
    study_id: str
    body: AnnotationBody
    target: AnnotationTarget
    created_by: str
    visible_to: list[str] | None
    version: int
    created_at: str
    updated_at: str


class AnnotationUpdate(BaseModel):
    body: AnnotationBody | None = None
    target: AnnotationTarget | None = None
    visible_to: list[str] | None = None
    version: int = Field(..., description="Must match current version (optimistic lock)")


def _row_to_out(row: dict) -> AnnotationOut:
    return AnnotationOut(
        id=row["id"],
        slide_id=row["slide_id"],
        study_id=row["study_id"],
        body=AnnotationBody(**json.loads(row["body"])),
        target=AnnotationTarget(**json.loads(row["target"])),
        created_by=row["created_by"],
        visible_to=_decode_visible_to(row["visible_to"]),
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def _init_sqlite(path: str) -> None:
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await _apply_sqlite_pragmas(db)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS annotations (
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
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ann_slide_study ON annotations(slide_id, study_id)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ann_slide ON annotations(slide_id)")
        await db.commit()


async def _init_postgres(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
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
    finally:
        await conn.close()


async def init_db(db_path: str | None = None, db_url: str | None = None) -> None:
    global _db_path, _db_url
    _db_path = db_path or settings.annotation_db_path
    _db_url = db_url or _settings_db_url()
    if _get_db_url():
        await _init_postgres(_get_db_url())
        logger.info("Annotation DB ready (postgres)")
    else:
        await _init_sqlite(_get_db_path())
        logger.info("Annotation DB ready (sqlite): %s", _get_db_path())


async def _list_sqlite(slide_id: str, study_id: str, user_sub: str) -> list[dict]:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM annotations
            WHERE slide_id = ? AND study_id = ?
              AND (created_by = ? OR visible_to IS NOT NULL)
            """,
            (slide_id, study_id, user_sub),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _list_postgres(slide_id: str, study_id: str, user_sub: str) -> list[dict]:
    conn = await asyncpg.connect(_get_db_url())
    try:
        rows = await conn.fetch(
            """
            SELECT
                id, slide_id, study_id, body, target, created_by, visible_to,
                version,
                to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at,
                to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS updated_at
            FROM annotations
            WHERE slide_id = $1 AND study_id = $2
              AND (created_by = $3 OR visible_to IS NOT NULL)
            """,
            slide_id,
            study_id,
            user_sub,
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


async def _create_sqlite(data: AnnotationIn, user_sub: str) -> dict:
    ann_id = str(uuid.uuid4())
    visible_to_json = json.dumps(data.visible_to) if data.visible_to is not None else None
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
            INSERT INTO annotations (id, slide_id, study_id, body, target, created_by, visible_to)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ann_id,
                data.slide_id,
                data.study_id,
                data.body.model_dump_json(),
                _sqlite_target_json(data.target.selector),
                user_sub,
                visible_to_json,
            ),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM annotations WHERE id = ?", (ann_id,))
        row = await cursor.fetchone()
    return dict(row)


async def _create_postgres(data: AnnotationIn, user_sub: str) -> dict:
    ann_id = str(uuid.uuid4())
    visible_to_json = json.dumps(data.visible_to) if data.visible_to is not None else None
    conn = await asyncpg.connect(_get_db_url())
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO annotations (id, slide_id, study_id, body, target, created_by, visible_to)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING
                id, slide_id, study_id, body, target, created_by, visible_to, version,
                to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at,
                to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS updated_at
            """,
            ann_id,
            data.slide_id,
            data.study_id,
            data.body.model_dump_json(),
            _sqlite_target_json(data.target.selector),
            user_sub,
            visible_to_json,
        )
        return dict(row)
    finally:
        await conn.close()


async def _get_existing_sqlite(annotation_id: str) -> dict | None:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, slide_id, study_id, body, target, created_by, visible_to, version, created_at"
            " FROM annotations WHERE id = ?",
            (annotation_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _get_existing_postgres(annotation_id: str) -> dict | None:
    conn = await asyncpg.connect(_get_db_url())
    try:
        row = await conn.fetchrow(
            """
            SELECT
                id, slide_id, study_id, body, target, created_by, visible_to, version,
                to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at
            FROM annotations WHERE id = $1
            """,
            annotation_id,
        )
        return dict(row) if row else None
    finally:
        await conn.close()


async def _update_sqlite(annotation_id: str, new_body: str, new_target: str, new_visible: str | None, new_version: int) -> None:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        await db.execute(
            """
            UPDATE annotations
            SET body = ?, target = ?, visible_to = ?, version = ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            (new_body, new_target, new_visible, new_version, annotation_id),
        )
        await db.commit()


async def _update_postgres(annotation_id: str, new_body: str, new_target: str, new_visible: str | None, new_version: int) -> str:
    conn = await asyncpg.connect(_get_db_url())
    try:
        row = await conn.fetchrow(
            """
            UPDATE annotations
            SET body = $1, target = $2, visible_to = $3, version = $4,
                updated_at = timezone('utc', now())
            WHERE id = $5
            RETURNING to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
            """,
            new_body,
            new_target,
            new_visible,
            new_version,
            annotation_id,
        )
        return row["to_char"]
    finally:
        await conn.close()


async def _delete_sqlite(annotation_id: str) -> None:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        await db.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
        await db.commit()


async def _delete_postgres(annotation_id: str) -> None:
    conn = await asyncpg.connect(_get_db_url())
    try:
        await conn.execute("DELETE FROM annotations WHERE id = $1", annotation_id)
    finally:
        await conn.close()


@router.get("", response_model=list[AnnotationOut])
async def list_annotations(
    slide_id: str = Query(..., description="Slide image_id"),
    study_id: str = Query(..., description="cBioPortal study ID"),
    user: dict = Depends(require_user),
) -> list[AnnotationOut]:
    user_sub: str = user["sub"]
    user_groups: set[str] = set(user["groups"])
    if _storage_kind() == "postgres":
        rows = await _list_postgres(slide_id, study_id, user_sub)
    else:
        rows = await _list_sqlite(slide_id, study_id, user_sub)
    return [_row_to_out(row) for row in rows if _is_visible(row, user_sub, user_groups)]


@router.post("", response_model=AnnotationOut, status_code=status.HTTP_201_CREATED)
async def create_annotation(
    data: AnnotationIn,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    if _storage_kind() == "postgres":
        row = await _create_postgres(data, user["sub"])
    else:
        row = await _create_sqlite(data, user["sub"])
    return _row_to_out(row)


@router.put("/{annotation_id}", response_model=AnnotationOut)
async def update_annotation(
    annotation_id: str,
    data: AnnotationUpdate,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    existing = (
        await _get_existing_postgres(annotation_id)
        if _storage_kind() == "postgres"
        else await _get_existing_sqlite(annotation_id)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if existing["created_by"] != user["sub"]:
        raise HTTPException(status_code=403, detail="Only the creator may update this annotation")
    if existing["version"] != data.version:
        raise HTTPException(
            status_code=409,
            detail=f"Version conflict: expected {existing['version']}, got {data.version}",
        )

    new_body = data.body.model_dump_json() if data.body is not None else existing["body"]
    new_target = (
        _sqlite_target_json(data.target.selector) if data.target is not None else existing["target"]
    )
    new_visible = json.dumps(data.visible_to) if data.visible_to is not None else existing["visible_to"]
    new_version = existing["version"] + 1
    if _storage_kind() == "postgres":
        updated_at = await _update_postgres(
            annotation_id, new_body, new_target, new_visible, new_version
        )
    else:
        await _update_sqlite(annotation_id, new_body, new_target, new_visible, new_version)
        updated_at = ""

    return AnnotationOut(
        id=existing["id"],
        slide_id=existing["slide_id"],
        study_id=existing["study_id"],
        body=AnnotationBody(**json.loads(new_body)),
        target=AnnotationTarget(**json.loads(new_target)),
        created_by=existing["created_by"],
        visible_to=_decode_visible_to(new_visible),
        version=new_version,
        created_at=existing["created_at"],
        updated_at=updated_at,
    )


@router.delete("/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(
    annotation_id: str,
    user: dict = Depends(require_user),
) -> None:
    existing = (
        await _get_existing_postgres(annotation_id)
        if _storage_kind() == "postgres"
        else await _get_existing_sqlite(annotation_id)
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Annotation not found")
    if existing["created_by"] != user["sub"]:
        raise HTTPException(status_code=403, detail="Only the creator may delete this annotation")

    if _storage_kind() == "postgres":
        await _delete_postgres(annotation_id)
    else:
        await _delete_sqlite(annotation_id)
