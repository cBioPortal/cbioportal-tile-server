"""Comment threads on annotations.

Routes (each with ``?studyId=<study>``):
  GET    /annotations/{id}/comments        the thread, oldest first
  POST   /annotations/{id}/comments        add ``{"body"}``
  PUT    /annotations/{id}/comments/{cid}  edit ``{"body"}`` (author only)
  DELETE /annotations/{id}/comments/{cid}  soft-delete (author only)

Anyone who can see the annotation in the authorized study may read its
thread and add to it: the annotation must be in that study (403 otherwise)
and visible to the user (``visible_to``; 404 otherwise, as for a missing
annotation).  Editing and deleting a comment stay with its author, who must
still be able to see the annotation.  A deleted comment is kept, flagged
``deleted``, and left out of the thread and of the annotation's
``comment_count``.  Deleting the annotation deletes its thread.

The thread is separate from the annotation's own note (``body.comment``),
which only the annotation's creator edits.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field, field_validator

from . import annotations as annotation_store
from .annotation_sql import Transaction
from .request_identity import require_same_study, require_user

router = APIRouter(prefix="/annotations", tags=["annotation comments"])

# Longest comment body, in characters.
COMMENT_MAX_CHARS = 4000

CommentId = Annotated[str, Path(min_length=1, max_length=128)]


class CommentIn(BaseModel):
    body: str = Field(..., min_length=1, max_length=COMMENT_MAX_CHARS)

    @field_validator("body")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A comment needs some text")
        return value


class CommentOut(BaseModel):
    id: str
    annotation_id: str
    study_id: str
    author: str
    body: str
    created_at: str
    updated_at: str


def _columns(tx: Transaction) -> str:
    return (
        "c.id, c.annotation_id, c.study_id, c.author, c.body,"
        f" {tx.ts('c.created_at')}, {tx.ts('c.updated_at')}"
    )


async def _visible_annotation(
    tx: Transaction, annotation_id: str, user: dict, lock: bool = False
) -> dict:
    """The annotation, if ``user`` may see it in the authorized study."""
    row = await tx.fetchrow(
        "SELECT a.id, a.study_id, a.created_by, a.visible_to FROM annotations a"
        f" WHERE a.id = $1{tx.for_update if lock else ''}",
        annotation_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Annotation not found")
    require_same_study(user, row["study_id"], "the annotation's study")
    if not annotation_store._is_visible(row, user["sub"], set(user["groups"])):
        raise HTTPException(status_code=404, detail="Annotation not found")
    return row


async def _own_comment(
    tx: Transaction, annotation_id: str, comment_id: str, user: dict, verb: str
) -> dict:
    """The live comment ``comment_id`` on the annotation, if ``user`` wrote it."""
    await _visible_annotation(tx, annotation_id, user)
    row = await tx.fetchrow(
        f"SELECT {_columns(tx)} FROM annotation_comments c"
        " WHERE c.id = $1 AND c.annotation_id = $2 AND c.deleted = FALSE"
        f"{tx.for_update}",
        comment_id,
        annotation_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Comment not found")
    if not annotation_store._can_modify({"created_by": row["author"]}, user):
        raise HTTPException(
            status_code=403, detail=f"Only the author may {verb} this comment"
        )
    return row


@router.get("/{annotation_id}/comments", response_model=list[CommentOut])
async def list_comments(
    annotation_id: CommentId,
    user: dict = Depends(require_user),
) -> list[CommentOut]:
    async with annotation_store.storage_transaction() as tx:
        await _visible_annotation(tx, annotation_id, user)
        rows = await tx.fetch(
            f"SELECT {_columns(tx)} FROM annotation_comments c"
            " WHERE c.annotation_id = $1 AND c.deleted = FALSE"
            f" ORDER BY {tx.created_order('c')}",
            annotation_id,
        )
    return [CommentOut(**row) for row in rows]


@router.post(
    "/{annotation_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_comment(
    annotation_id: CommentId,
    data: CommentIn,
    user: dict = Depends(require_user),
) -> CommentOut:
    comment_id = str(uuid.uuid4())
    async with annotation_store.storage_transaction(write=True) as tx:
        annotation = await _visible_annotation(tx, annotation_id, user, lock=True)
        await tx.execute(
            "INSERT INTO annotation_comments"
            " (id, annotation_id, study_id, author, body, created_at, updated_at, deleted)"
            f" VALUES ($1, $2, $3, $4, $5, {tx.now}, {tx.now}, FALSE)",
            comment_id,
            annotation_id,
            annotation["study_id"],
            user["sub"],
            data.body,
        )
        row = await tx.fetchrow(
            f"SELECT {_columns(tx)} FROM annotation_comments c WHERE c.id = $1",
            comment_id,
        )
    return CommentOut(**row)


@router.put("/{annotation_id}/comments/{comment_id}", response_model=CommentOut)
async def update_comment(
    annotation_id: CommentId,
    comment_id: CommentId,
    data: CommentIn,
    user: dict = Depends(require_user),
) -> CommentOut:
    async with annotation_store.storage_transaction(write=True) as tx:
        await _own_comment(tx, annotation_id, comment_id, user, "edit")
        await tx.execute(
            f"UPDATE annotation_comments SET body = $1, updated_at = {tx.now}"
            " WHERE id = $2",
            data.body,
            comment_id,
        )
        row = await tx.fetchrow(
            f"SELECT {_columns(tx)} FROM annotation_comments c WHERE c.id = $1",
            comment_id,
        )
    return CommentOut(**row)


@router.delete(
    "/{annotation_id}/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_comment(
    annotation_id: CommentId,
    comment_id: CommentId,
    user: dict = Depends(require_user),
) -> None:
    async with annotation_store.storage_transaction(write=True) as tx:
        await _own_comment(tx, annotation_id, comment_id, user, "delete")
        await tx.execute(
            f"UPDATE annotation_comments SET deleted = TRUE, updated_at = {tx.now}"
            " WHERE id = $1",
            comment_id,
        )
