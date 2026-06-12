"""
Annotation CRUD — FastAPI router backed by SQLite (WAL mode).

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
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from .auth import require_user
from .config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotations", tags=["annotations"])

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

_db_path: str = ""


def _get_db_path() -> str:
    return _db_path or settings.annotation_db_path


async def init_db(db_path: str | None = None) -> None:
    """Create the annotations table (idempotent).  Call once at startup."""
    global _db_path
    path = db_path or settings.annotation_db_path
    _db_path = path
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode=WAL")
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
            "CREATE INDEX IF NOT EXISTS idx_ann_slide ON annotations(slide_id)"
        )
        await db.commit()
    logger.info("Annotation DB ready: %s", path)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class AnnotationBody(BaseModel):
    label: str = ""
    comment: str = ""
    type: str = ""


class AnnotationTarget(BaseModel):
    """W3C selector — store verbatim as JSON."""

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


# ---------------------------------------------------------------------------
# Visibility helpers
# ---------------------------------------------------------------------------


def _is_visible(row: dict, user_sub: str, user_groups: list[str]) -> bool:
    """Return True if this annotation is visible to the given user."""
    if row["created_by"] == user_sub:
        return True
    visible_to = row["visible_to"]
    if visible_to is None:
        return False
    groups = json.loads(visible_to)
    # Empty list means world-readable within study
    if not groups:
        return True
    return any(g in user_groups for g in groups)


def _row_to_out(row: dict) -> AnnotationOut:
    return AnnotationOut(
        id=row["id"],
        slide_id=row["slide_id"],
        study_id=row["study_id"],
        body=AnnotationBody(**json.loads(row["body"])),
        target=AnnotationTarget(**json.loads(row["target"])),
        created_by=row["created_by"],
        visible_to=json.loads(row["visible_to"]) if row["visible_to"] is not None else None,
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[AnnotationOut])
async def list_annotations(
    slide_id: str = Query(..., description="Slide image_id"),
    study_id: str = Query(..., description="cBioPortal study ID"),
    user: dict = Depends(require_user),
) -> list[AnnotationOut]:
    """Return all annotations for a slide that are visible to the calling user."""
    async with aiosqlite.connect(_get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM annotations WHERE slide_id = ? AND study_id = ?",
            (slide_id, study_id),
        )
        rows = await cursor.fetchall()

    result = []
    for row in rows:
        row_dict = dict(row)
        if _is_visible(row_dict, user["sub"], user["groups"]):
            result.append(_row_to_out(row_dict))
    return result


@router.post("", response_model=AnnotationOut, status_code=status.HTTP_201_CREATED)
async def create_annotation(
    data: AnnotationIn,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    """Create a new annotation owned by the calling user."""
    ann_id = str(uuid.uuid4())
    visible_to_json = json.dumps(data.visible_to) if data.visible_to is not None else None

    async with aiosqlite.connect(_get_db_path()) as db:
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
                json.dumps({"selector": data.target.selector}),
                user["sub"],
                visible_to_json,
            ),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM annotations WHERE id = ?", (ann_id,))
        row = await cursor.fetchone()

    return _row_to_out(dict(row))


@router.put("/{annotation_id}", response_model=AnnotationOut)
async def update_annotation(
    annotation_id: str,
    data: AnnotationUpdate,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    """
    Update an annotation.  ``data.version`` must match the stored version
    (optimistic concurrency control).  Only the creator may update.
    """
    async with aiosqlite.connect(_get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
        )
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Annotation not found")

        existing = dict(existing)
        if existing["created_by"] != user["sub"]:
            raise HTTPException(status_code=403, detail="Only the creator may update this annotation")
        if existing["version"] != data.version:
            raise HTTPException(
                status_code=409,
                detail=f"Version conflict: expected {existing['version']}, got {data.version}",
            )

        new_body = data.body.model_dump_json() if data.body is not None else existing["body"]
        new_target = (
            json.dumps({"selector": data.target.selector})
            if data.target is not None
            else existing["target"]
        )
        new_visible = (
            json.dumps(data.visible_to)
            if data.visible_to is not None
            else existing["visible_to"]
        )
        new_version = existing["version"] + 1

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
        cursor = await db.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,))
        row = await cursor.fetchone()

    return _row_to_out(dict(row))


@router.delete("/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(
    annotation_id: str,
    user: dict = Depends(require_user),
) -> None:
    """Delete an annotation.  Only the creator may delete."""
    async with aiosqlite.connect(_get_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT created_by FROM annotations WHERE id = ?", (annotation_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Annotation not found")
        if dict(row)["created_by"] != user["sub"]:
            raise HTTPException(status_code=403, detail="Only the creator may delete this annotation")

        await db.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
        await db.commit()
