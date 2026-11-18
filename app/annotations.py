"""
Annotation CRUD with pluggable SQLite or Postgres storage.

Routes (each with ``?studyId=<study>``):
  GET    /annotations?slide_id=   list a slide's annotations in the study
  POST   /annotations             create a new annotation
  POST   /annotations/batch       create up to 100 in one study, all or none
  PUT    /annotations/{id}        update (creator only; optimistic concurrency via version)
  POST   /annotations/{id}/review accept or reject a machine-made annotation
  DELETE /annotations/{id}        delete (creator only) with its comment thread
  POST   /annotations/batch-update  update up to 100 under the PUT rules, all or none
  POST   /annotations/batch-delete  delete up to 100 (creator only), all or none
  POST   /annotations/batch-review  review up to 100 under the review rules, all or none

Comment threads (``app.annotation_comments``) live under
``/annotations/{id}/comments``; the list reports each row's ``comment_count``.

Batches: every item names an ``id`` and its ``version``; an id may appear
once.  The rows are read and locked, every item is checked under the
single-item rules, and only then is anything written, all in one
transaction.  The first missing, forbidden or invalid item fails the batch
(404/403/422 naming the item's index); stale versions are reported
together (409).  A 2xx answers the rows in request order.

Auth: ``require_user()`` takes the user and study the authenticating proxy
asserted (``app.request_identity``).  The body's study, and the stored
row's study for an update or delete, must be that study; ``created_by`` is
the proxy's user.  These routes are served by ``app.companion`` only.

Review: an annotation whose ``body.provenance.source`` names a machine
source (``MACHINE_SOURCES``: the assistant or a model) may be accepted or
rejected by any user who can see it in its study, not only its creator.
A review records ``review_status``, ``reviewed_by`` and ``reviewed_at`` in
the provenance and bumps the version.  A rejected annotation is kept, for
the audit trail, and left out of the list unless ``include_rejected=true``.
Its creator may still delete it.

Provenance trust: over HTTP, only a trusted writer (a user listed in
``ANNOTATION_TRUSTED_WRITERS``, such as a bulk loader's service identity)
may create an annotation whose ``provenance.source`` is a machine source,
or one that carries review fields; anyone else gets 422.  An update may not
turn an annotation machine-made or change its machine source (422), and the
review fields always keep their stored values: only the review route sets
them.  The assistant writes its annotations in-process
(``app.agent._commit_annotations``), not through these routes, so it needs
no entry.  Stored rows are never re-checked, so existing machine-made rows
stay readable, reviewable and editable by their creator.

``slide_id`` carries the opaque slide key, never a pathology image ID; any
other value is rejected so image IDs cannot reach annotation storage.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import aiosqlite
import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from . import annotation_sql
from .request_identity import require_same_study, require_user
from .sealed_source import SLIDE_KEY_PATTERN
from .config import settings
from .annotation_db import close_pool, connection, migrate, open_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/annotations", tags=["annotations"])

SLIDE_KEY_REGEX = SLIDE_KEY_PATTERN.pattern

# The live comments in annotation ``a``'s thread.
COMMENT_COUNT_SQL = (
    "(SELECT COUNT(*) FROM annotation_comments c"
    " WHERE c.annotation_id = a.id AND c.deleted = FALSE)"
)

_db_path: str = ""
_db_url: str = ""


def _settings_db_url() -> str:
    return getattr(settings, "annotation_database_url", "")


def _storage_kind() -> str:
    return "postgres" if (_db_url or _settings_db_url()) else "sqlite"


def storage_kind() -> str:
    """``postgres`` or ``sqlite``: where annotations are stored."""
    return _storage_kind()


async def ping() -> None:
    """Run a trivial query against annotation storage; raises if unreachable."""
    if _storage_kind() == "postgres":
        async with connection(_get_db_url()) as conn:
            await conn.fetchval("SELECT 1")
    else:
        async with aiosqlite.connect(_get_db_path()) as db:
            await db.execute("SELECT 1 FROM annotations LIMIT 1")


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


def _can_modify(row: dict, user: dict) -> bool:
    if row["created_by"] == user["sub"]:
        return True
    return bool(
        settings.annotation_local_development
        and user.get("sub") == "local-development"
    )


# ``provenance.source`` values of annotations a machine made: the research
# assistant ("agent"/"assistant") or a model ("model", "ai", and
# "precomputed" for model output loaded in bulk).
MACHINE_SOURCES = frozenset({"agent", "assistant", "ai", "model", "precomputed"})
REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"
REVIEW_FIELDS = ("review_status", "reviewed_by", "reviewed_at")


def _provenance(body: Any) -> dict[str, Any]:
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return {}
    provenance = body.get("provenance") if isinstance(body, dict) else None
    return provenance if isinstance(provenance, dict) else {}


def is_machine_made(body: Any) -> bool:
    """Whether a body's provenance names a machine source (``MACHINE_SOURCES``)."""
    source = _provenance(body).get("source")
    return isinstance(source, str) and source.strip().lower() in MACHINE_SOURCES


def _machine_source(provenance: dict[str, Any]) -> str | None:
    source = provenance.get("source")
    if isinstance(source, str) and source.strip().lower() in MACHINE_SOURCES:
        return source.strip().lower()
    return None


def is_trusted_writer(user: dict) -> bool:
    """Whether ``user`` may write machine provenance over HTTP."""
    return user.get("sub") in set(getattr(settings, "annotation_trusted_writers", []) or [])


def _require_untrusted_provenance_allowed(body: "AnnotationBody", user: dict) -> None:
    """422 unless ``user`` may create an annotation with this provenance."""
    if is_trusted_writer(user):
        return
    provenance = body.provenance or {}
    if _machine_source(provenance) is not None:
        raise HTTPException(
            status_code=422,
            detail="Only a trusted writer may create a machine-made annotation",
        )
    if any(name in provenance for name in REVIEW_FIELDS):
        raise HTTPException(
            status_code=422,
            detail="Review fields are set only by the review route",
        )


def _guard_updated_provenance(
    existing_body: Any, body: "AnnotationBody", user: dict
) -> "AnnotationBody":
    """The body to store for an update by ``user``.

    An untrusted caller may not make an annotation machine-made or change
    its machine source (422); the review fields keep their stored values.
    """
    if is_trusted_writer(user) or body.provenance is None:
        return body
    stored = _provenance(existing_body)
    provenance = dict(body.provenance)
    new_source = _machine_source(provenance)
    if new_source is not None and new_source != _machine_source(stored):
        raise HTTPException(
            status_code=422,
            detail="Only a trusted writer may mark an annotation machine-made",
        )
    for name in REVIEW_FIELDS:
        provenance.pop(name, None)
        if name in stored:
            provenance[name] = stored[name]
    return body.model_copy(update={"provenance": provenance})


def is_rejected(body: Any) -> bool:
    """Whether a machine-made annotation was rejected in review."""
    return is_machine_made(body) and _provenance(body).get("review_status") == REVIEW_REJECTED


class AnnotationBody(BaseModel):
    label: str = ""
    comment: str = ""
    type: str = ""
    layer_name: str = "Default"
    color: str = "#2f80ed"
    provenance: dict[str, Any] | None = None


class AnnotationTarget(BaseModel):
    selector: dict[str, Any]


class AnnotationIn(BaseModel):
    slide_id: str = Field(..., pattern=SLIDE_KEY_REGEX, description="Opaque slide key")
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
    comment_count: int = Field(
        default=0, description="Comments in the annotation's thread (not deleted)"
    )


class AnnotationUpdate(BaseModel):
    body: AnnotationBody | None = None
    target: AnnotationTarget | None = None
    visible_to: list[str] | None = None
    version: int = Field(
        ..., description="Must match current version (optimistic lock)"
    )


class AnnotationReview(BaseModel):
    decision: Literal["accept", "reject"]
    version: int = Field(
        ..., description="Must match current version (optimistic lock)"
    )


class AnnotationBatchItem(BaseModel):
    body: AnnotationBody
    target: AnnotationTarget
    visible_to: list[str] | None = None


class AnnotationBatchIn(BaseModel):
    slide_id: str | None = None
    study_id: str | None = None
    annotations: list[AnnotationIn | AnnotationBatchItem] = Field(
        min_length=1, max_length=100
    )


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
        comment_count=int(row.get("comment_count") or 0),
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
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ann_slide ON annotations(slide_id)"
        )
        # Schema version 2 (see app.annotation_db): comment threads and
        # slide-level notes.  Timestamps are UTC ISO-8601 text.
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS annotation_comments (
                id            TEXT PRIMARY KEY,
                annotation_id TEXT NOT NULL,
                study_id      TEXT NOT NULL,
                author        TEXT NOT NULL,
                body          TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                deleted       INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ann_comments_annotation "
            "ON annotation_comments(annotation_id, created_at)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS slide_notes (
                id          TEXT PRIMARY KEY,
                slide_id    TEXT NOT NULL,
                study_id    TEXT NOT NULL,
                text        TEXT NOT NULL DEFAULT '',
                tags        TEXT NOT NULL DEFAULT '[]',
                created_by  TEXT NOT NULL,
                visible_to  TEXT,
                version     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_slide_notes_slide_study "
            "ON slide_notes(slide_id, study_id)"
        )
        await db.commit()


async def _init_postgres(dsn: str) -> None:
    await open_pool(dsn)
    async with connection(dsn) as conn:
        await migrate(conn)


async def init_db(db_path: str | None = None, db_url: str | None = None) -> None:
    global _db_path, _db_url
    _db_path = db_path or settings.annotation_db_path
    _db_url = db_url or _settings_db_url()
    if _get_db_url():
        await _init_postgres(_get_db_url())
        logger.info("Annotation DB ready (postgres)")
    else:
        await close_pool()
        await _init_sqlite(_get_db_path())
        logger.info("Annotation DB ready (sqlite): %s", _get_db_path())


async def close_db() -> None:
    await close_pool()


async def _list_sqlite(slide_id: str, study_id: str, user_sub: str) -> list[dict]:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""
            SELECT a.*, {COMMENT_COUNT_SQL} AS comment_count
            FROM annotations a
            WHERE a.slide_id = ? AND a.study_id = ?
              AND (a.created_by = ? OR a.visible_to IS NOT NULL)
            """,
            (slide_id, study_id, user_sub),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def _list_postgres(slide_id: str, study_id: str, user_sub: str) -> list[dict]:
    async with connection(_get_db_url()) as conn:
        rows = await conn.fetch(
            f"""
            SELECT
                a.id, a.slide_id, a.study_id, a.body, a.target, a.created_by, a.visible_to,
                a.version,
                to_char(a.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at,
                to_char(a.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS updated_at,
                {COMMENT_COUNT_SQL} AS comment_count
            FROM annotations a
            WHERE a.slide_id = $1 AND a.study_id = $2
              AND (a.created_by = $3 OR a.visible_to IS NOT NULL)
            """,
            slide_id,
            study_id,
            user_sub,
        )
        return [dict(row) for row in rows]


async def _create_sqlite(data: AnnotationIn, user_sub: str) -> dict:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        row = await _insert_sqlite_connection(db, data, user_sub)
        await db.commit()
    return row


async def _insert_sqlite_connection(
    db: aiosqlite.Connection,
    data: AnnotationIn,
    user_sub: str,
    annotation_id: str | None = None,
) -> dict:
    ann_id = annotation_id or str(uuid.uuid4())
    visible_to_json = (
        json.dumps(data.visible_to) if data.visible_to is not None else None
    )
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
    cursor = await db.execute("SELECT * FROM annotations WHERE id = ?", (ann_id,))
    row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Annotation insert did not return a row")
    return dict(row)


async def _create_postgres(data: AnnotationIn, user_sub: str) -> dict:
    async with connection(_get_db_url()) as conn:
        return await _insert_postgres_connection(conn, data, user_sub)


async def _insert_postgres_connection(
    conn: asyncpg.Connection,
    data: AnnotationIn,
    user_sub: str,
    annotation_id: str | None = None,
) -> dict:
    ann_id = annotation_id or str(uuid.uuid4())
    visible_to_json = (
        json.dumps(data.visible_to) if data.visible_to is not None else None
    )
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
    if row is None:
        raise RuntimeError("Annotation insert did not return a row")
    return dict(row)


async def _get_existing_sqlite(annotation_id: str) -> dict | None:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT a.id, a.slide_id, a.study_id, a.body, a.target, a.created_by,"
            f" a.visible_to, a.version, a.created_at, {COMMENT_COUNT_SQL} AS comment_count"
            " FROM annotations a WHERE a.id = ?",
            (annotation_id,),
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def _get_existing_postgres(annotation_id: str) -> dict | None:
    async with connection(_get_db_url()) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT
                a.id, a.slide_id, a.study_id, a.body, a.target, a.created_by, a.visible_to, a.version,
                to_char(a.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') AS created_at,
                {COMMENT_COUNT_SQL} AS comment_count
            FROM annotations a WHERE a.id = $1
            """,
            annotation_id,
        )
        return dict(row) if row else None


async def _update_sqlite(
    annotation_id: str,
    new_body: str,
    new_target: str,
    new_visible: str | None,
    expected_version: int,
    new_version: int,
) -> str | None:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        cursor = await db.execute(
            """
            UPDATE annotations
            SET body = ?, target = ?, visible_to = ?, version = ?,
                updated_at = datetime('now')
            WHERE id = ? AND version = ?
            """,
            (
                new_body,
                new_target,
                new_visible,
                new_version,
                annotation_id,
                expected_version,
            ),
        )
        await db.commit()
        if cursor.rowcount != 1:
            return None
        cursor = await db.execute(
            "SELECT updated_at FROM annotations WHERE id = ?", (annotation_id,)
        )
        row = await cursor.fetchone()
    return row[0] if row else None


async def _update_postgres(
    annotation_id: str,
    new_body: str,
    new_target: str,
    new_visible: str | None,
    expected_version: int,
    new_version: int,
) -> str | None:
    async with connection(_get_db_url()) as conn:
        row = await conn.fetchrow(
            """
            UPDATE annotations
            SET body = $1, target = $2, visible_to = $3, version = $4,
                updated_at = timezone('utc', now())
            WHERE id = $5 AND version = $6
            RETURNING to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')
            """,
            new_body,
            new_target,
            new_visible,
            new_version,
            annotation_id,
            expected_version,
        )
        return row["to_char"] if row else None


async def _delete_sqlite(annotation_id: str) -> None:
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
            await db.execute(
                "DELETE FROM annotation_comments WHERE annotation_id = ?", (annotation_id,)
            )
        except BaseException:
            await db.rollback()
            raise
        await db.commit()


async def _delete_postgres(annotation_id: str) -> None:
    async with connection(_get_db_url()) as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM annotations WHERE id = $1", annotation_id)
            await conn.execute(
                "DELETE FROM annotation_comments WHERE annotation_id = $1", annotation_id
            )


@router.get("", response_model=list[AnnotationOut])
async def list_annotations(
    slide_id: str = Query(..., pattern=SLIDE_KEY_REGEX, description="Opaque slide key"),
    user: dict = Depends(require_user),
    include_rejected: Annotated[
        bool,
        Query(description="Also list machine-made annotations rejected in review"),
    ] = False,
) -> list[AnnotationOut]:
    study_id: str = user["study_id"]
    user_sub: str = user["sub"]
    user_groups: set[str] = set(user["groups"])
    if _storage_kind() == "postgres":
        rows = await _list_postgres(slide_id, study_id, user_sub)
    else:
        rows = await _list_sqlite(slide_id, study_id, user_sub)
    return [
        _row_to_out(row)
        for row in rows
        if _is_visible(row, user_sub, user_groups)
        and (include_rejected or not is_rejected(row["body"]))
    ]


@router.post("", response_model=AnnotationOut, status_code=status.HTTP_201_CREATED)
async def create_annotation(
    data: AnnotationIn,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    require_same_study(user, data.study_id, "the annotation's study")
    _require_untrusted_provenance_allowed(data.body, user)
    if _storage_kind() == "postgres":
        row = await _create_postgres(data, user["sub"])
    else:
        row = await _create_sqlite(data, user["sub"])
    return _row_to_out(row)


@router.post("/batch", response_model=list[AnnotationOut], status_code=status.HTTP_201_CREATED)
async def create_annotation_batch(
    data: AnnotationBatchIn,
    user: dict = Depends(require_user),
) -> list[AnnotationOut]:
    """Create a bounded group of annotations in the authorized study.

    The items are inserted in one transaction, so a failure saves none of
    them, and the response lists the saved annotations in request order.
    """
    normalized: list[AnnotationIn] = []
    for annotation in data.annotations:
        if isinstance(annotation, AnnotationIn):
            normalized.append(annotation)
        elif data.slide_id and data.study_id:
            normalized.append(
                AnnotationIn(
                    slide_id=data.slide_id,
                    study_id=data.study_id,
                    body=annotation.body,
                    target=annotation.target,
                    visible_to=annotation.visible_to,
                )
            )
        else:
            raise HTTPException(
                status_code=422,
                detail="Batch items without slide_id/study_id require top-level scope",
            )
    study_ids = {annotation.study_id for annotation in normalized}
    if len(study_ids) != 1:
        raise HTTPException(status_code=422, detail="A batch must contain one study")
    study_id = next(iter(study_ids))
    require_same_study(user, study_id, "the batch's study")
    for annotation in normalized:
        _require_untrusted_provenance_allowed(annotation.body, user)
    if _storage_kind() == "postgres":
        rows = await _create_batch_postgres(normalized, user["sub"])
    else:
        rows = await _create_batch_sqlite(normalized, user["sub"])
    return [_row_to_out(row) for row in rows]


async def _create_batch_sqlite(items: list[AnnotationIn], user_sub: str) -> list[dict]:
    """Insert every item in one transaction: all are saved, or none."""
    async with aiosqlite.connect(_get_db_path()) as db:
        await _apply_sqlite_pragmas(db)
        await db.execute("BEGIN IMMEDIATE")
        try:
            rows = [await _insert_sqlite_connection(db, item, user_sub) for item in items]
        except BaseException:
            await db.rollback()
            raise
        await db.commit()
    return rows


async def _create_batch_postgres(items: list[AnnotationIn], user_sub: str) -> list[dict]:
    """Insert every item in one transaction: all are saved, or none."""
    async with connection(_get_db_url()) as conn:
        async with conn.transaction():
            return [await _insert_postgres_connection(conn, item, user_sub) for item in items]




# --- per-annotation rules, shared by the single and batch routes ------------


def _authorize_change(existing: dict, user: dict, verb: str) -> None:
    """403 unless ``user`` may update or delete ``existing`` (creator only)."""
    require_same_study(user, existing["study_id"], "the annotation's study")
    if not _can_modify(existing, user):
        raise HTTPException(
            status_code=403, detail=f"Only the creator may {verb} this annotation"
        )


def _authorize_review(existing: dict, user: dict) -> None:
    """403/404 unless ``user`` may review ``existing``."""
    require_same_study(user, existing["study_id"], "the annotation's study")
    if not _is_visible(existing, user["sub"], set(user["groups"])):
        raise HTTPException(status_code=404, detail="Annotation not found")
    if not is_machine_made(existing["body"]):
        raise HTTPException(
            status_code=403,
            detail="Only machine-made annotations can be reviewed",
        )


def _check_version(existing: dict, version: int) -> None:
    if existing["version"] != version:
        raise HTTPException(
            status_code=409,
            detail=f"Version conflict: expected {existing['version']}, got {version}",
        )


def _updated_fields(
    existing: dict, data: "AnnotationUpdate", user: dict
) -> tuple[str, str, str | None]:
    """The body, target and visible_to an update stores (422 on bad provenance)."""
    new_body = (
        _guard_updated_provenance(existing["body"], data.body, user).model_dump_json()
        if data.body is not None
        else existing["body"]
    )
    new_target = (
        _sqlite_target_json(data.target.selector)
        if data.target is not None
        else existing["target"]
    )
    if "visible_to" in data.model_fields_set:
        new_visible = (
            json.dumps(data.visible_to) if data.visible_to is not None else None
        )
    else:
        new_visible = existing["visible_to"]
    return new_body, new_target, new_visible


def _reviewed_body(existing: dict, decision: str, user: dict) -> str:
    """The stored body with the review fields of ``decision`` by ``user``."""
    body = json.loads(existing["body"])
    body["provenance"] = {
        **_provenance(body),
        "review_status": REVIEW_ACCEPTED if decision == "accept" else REVIEW_REJECTED,
        "reviewed_by": user["sub"],
        "reviewed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return AnnotationBody(**body).model_dump_json()


async def _get_existing(annotation_id: str) -> dict | None:
    if _storage_kind() == "postgres":
        return await _get_existing_postgres(annotation_id)
    return await _get_existing_sqlite(annotation_id)


@router.put("/{annotation_id}", response_model=AnnotationOut)
async def update_annotation(
    annotation_id: str,
    data: AnnotationUpdate,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    existing = await _get_existing(annotation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Annotation not found")
    _authorize_change(existing, user, "update")
    _check_version(existing, data.version)
    new_body, new_target, new_visible = _updated_fields(existing, data, user)
    return await _write_version(existing, new_body, new_target, new_visible)


async def _write_version(
    existing: dict, new_body: str, new_target: str, new_visible: str | None
) -> AnnotationOut:
    """Store the next version of ``existing``; 409 if it changed meanwhile."""
    new_version = existing["version"] + 1
    write = _update_postgres if _storage_kind() == "postgres" else _update_sqlite
    updated_at = await write(
        existing["id"],
        new_body,
        new_target,
        new_visible,
        existing["version"],
        new_version,
    )
    if updated_at is None:
        raise HTTPException(
            status_code=409,
            detail="Annotation was modified concurrently",
        )

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
        comment_count=int(existing.get("comment_count") or 0),
    )


@router.post("/{annotation_id}/review", response_model=AnnotationOut)
async def review_annotation(
    annotation_id: str,
    data: AnnotationReview,
    user: dict = Depends(require_user),
) -> AnnotationOut:
    """Accept or reject a machine-made annotation.

    Any user who can see the annotation in the authorized study may review
    it; the creator-only rule still applies to PUT and DELETE.  Only the
    provenance's review fields change.  A rejected annotation is kept and
    left out of the list (see ``list_annotations``).
    """
    existing = await _get_existing(annotation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Annotation not found")
    _authorize_review(existing, user)
    _check_version(existing, data.version)
    new_body = _reviewed_body(existing, data.decision, user)
    return await _write_version(
        existing, new_body, existing["target"], existing["visible_to"]
    )


@router.delete("/{annotation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_annotation(
    annotation_id: str,
    user: dict = Depends(require_user),
) -> None:
    """Delete an annotation and its comment thread (creator only)."""
    existing = await _get_existing(annotation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Annotation not found")
    _authorize_change(existing, user, "delete")

    if _storage_kind() == "postgres":
        await _delete_postgres(annotation_id)
    else:
        await _delete_sqlite(annotation_id)


# --- batch update, delete and review ----------------------------------------

BATCH_MAX_ITEMS = 100

AnnotationId = Annotated[str, Field(min_length=1, max_length=128)]


class AnnotationBatchUpdateItem(AnnotationUpdate):
    id: AnnotationId


class AnnotationBatchVersionItem(BaseModel):
    id: AnnotationId
    version: int = Field(..., description="Must match current version (optimistic lock)")


class AnnotationBatchReviewItem(AnnotationReview):
    id: AnnotationId


class AnnotationBatchUpdate(BaseModel):
    items: list[AnnotationBatchUpdateItem] = Field(min_length=1, max_length=BATCH_MAX_ITEMS)


class AnnotationBatchDelete(BaseModel):
    items: list[AnnotationBatchVersionItem] = Field(min_length=1, max_length=BATCH_MAX_ITEMS)


class AnnotationBatchReview(BaseModel):
    items: list[AnnotationBatchReviewItem] = Field(min_length=1, max_length=BATCH_MAX_ITEMS)


def _item_loc(index: int) -> list[Any]:
    return ["body", "items", index]


def _item_error(exc: HTTPException, index: int, annotation_id: str) -> HTTPException:
    """``exc`` from the single-item rules, naming the batch item it is about.

    A 422 keeps FastAPI's validation shape (a list of ``{loc, msg, type}``);
    403 and 404 answer ``{msg, index, id, loc}``.
    """
    if exc.status_code == 422:
        return HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": _item_loc(index),
                    "msg": str(exc.detail),
                    "type": "value_error",
                    "id": annotation_id,
                }
            ],
        )
    return HTTPException(
        status_code=exc.status_code,
        detail={
            "msg": str(exc.detail),
            "index": index,
            "id": annotation_id,
            "loc": _item_loc(index),
        },
    )


def _require_unique_ids(items: list[Any]) -> None:
    seen: set[str] = set()
    for index, item in enumerate(items):
        if item.id in seen:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": [*_item_loc(index), "id"],
                        "msg": "An annotation may appear once in a batch",
                        "type": "value_error",
                        "id": item.id,
                    }
                ],
            )
        seen.add(item.id)


def storage_transaction(write: bool = False):
    """One transaction on annotation storage (``app.annotation_sql``)."""
    return annotation_sql.transaction(
        _storage_kind(),
        db_path=_get_db_path,
        db_url=_get_db_url,
        pragmas=_apply_sqlite_pragmas,
        write=write,
    )


def _annotation_columns(tx: annotation_sql.Transaction) -> str:
    return (
        "a.id, a.slide_id, a.study_id, a.body, a.target, a.created_by, a.visible_to,"
        f" a.version, {tx.ts('a.created_at')}, {tx.ts('a.updated_at')},"
        f" {COMMENT_COUNT_SQL} AS comment_count"
    )


def _annotation_now(tx: annotation_sql.Transaction) -> str:
    """``updated_at`` for an annotation write, as the single-item routes set it."""
    return "timezone('utc', now())" if tx.kind == "postgres" else "datetime('now')"


async def _locked_rows(tx: annotation_sql.Transaction, ids: list[str]) -> dict[str, dict]:
    """The stored rows for ``ids``, locked for this transaction.

    Rows are locked in id order, so two batches touching the same rows
    cannot deadlock.
    """
    rows: dict[str, dict] = {}
    for annotation_id in sorted(set(ids)):
        row = await tx.fetchrow(
            f"SELECT {_annotation_columns(tx)} FROM annotations a WHERE a.id = $1"
            f"{tx.for_update}",
            annotation_id,
        )
        if row is not None:
            rows[annotation_id] = row
    return rows


async def _apply_batch(
    kind: Literal["update", "delete", "review"],
    items: list[Any],
    user: dict,
) -> list[AnnotationOut]:
    """Check every item, then write them all in one transaction.

    The first item that is missing, forbidden or invalid fails the batch
    (404/403/422 naming it); stale versions are collected and answered
    together (409).  Nothing is written unless every item passes.
    """
    _require_unique_ids(items)
    async with storage_transaction(write=True) as tx:
        rows = await _locked_rows(tx, [item.id for item in items])
        conflicts: list[dict[str, Any]] = []
        writes: list[tuple[int, Any, dict, tuple[str, str, str | None] | None]] = []
        for index, item in enumerate(items):
            existing = rows.get(item.id)
            try:
                if existing is None:
                    raise HTTPException(status_code=404, detail="Annotation not found")
                if kind == "review":
                    _authorize_review(existing, user)
                else:
                    _authorize_change(existing, user, kind)
                if existing["version"] != item.version:
                    conflicts.append(
                        {
                            "index": index,
                            "id": item.id,
                            "loc": [*_item_loc(index), "version"],
                            "version": item.version,
                            "current_version": existing["version"],
                        }
                    )
                    continue
                if kind == "update":
                    fields = _updated_fields(existing, item, user)
                elif kind == "review":
                    fields = (
                        _reviewed_body(existing, item.decision, user),
                        existing["target"],
                        existing["visible_to"],
                    )
                else:
                    fields = None
            except HTTPException as exc:
                raise _item_error(exc, index, item.id) from None
            writes.append((index, item, existing, fields))
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail={
                    "msg": "Version conflict: "
                    + ", ".join(
                        f"item {c['index']} ({c['id']}) is at version"
                        f" {c['current_version']}, not {c['version']}"
                        for c in conflicts
                    ),
                    "conflicts": conflicts,
                },
            )

        for index, item, existing, fields in writes:
            if fields is None:
                changed = await tx.execute(
                    "DELETE FROM annotations WHERE id = $1 AND version = $2",
                    item.id,
                    existing["version"],
                )
                await tx.execute(
                    "DELETE FROM annotation_comments WHERE annotation_id = $1", item.id
                )
            else:
                changed = await tx.execute(
                    "UPDATE annotations SET body = $1, target = $2, visible_to = $3,"
                    f" version = version + 1, updated_at = {_annotation_now(tx)}"
                    " WHERE id = $4 AND version = $5",
                    *fields,
                    item.id,
                    existing["version"],
                )
            if changed != 1:
                raise _item_error(
                    HTTPException(status_code=409, detail="Annotation was modified concurrently"),
                    index,
                    item.id,
                )

        if kind == "delete":
            return [_row_to_out(existing) for _, _, existing, _ in writes]
        updated = await _locked_rows(tx, [item.id for item in items])
    return [_row_to_out(updated[item.id]) for item in items]


@router.post("/batch-update", response_model=list[AnnotationOut])
async def update_annotation_batch(
    data: AnnotationBatchUpdate,
    user: dict = Depends(require_user),
) -> list[AnnotationOut]:
    """Update 1–100 annotations under the PUT rules, all or none."""
    return await _apply_batch("update", data.items, user)


@router.post("/batch-delete", response_model=list[AnnotationOut])
async def delete_annotation_batch(
    data: AnnotationBatchDelete,
    user: dict = Depends(require_user),
) -> list[AnnotationOut]:
    """Delete 1–100 annotations (creator only), all or none.

    Answers the deleted annotations as they were stored.
    """
    return await _apply_batch("delete", data.items, user)


@router.post("/batch-review", response_model=list[AnnotationOut])
async def review_annotation_batch(
    data: AnnotationBatchReview,
    user: dict = Depends(require_user),
) -> list[AnnotationOut]:
    """Review 1–100 machine-made annotations under the review rules, all or none."""
    return await _apply_batch("review", data.items, user)
