"""Slide-level notes and tags: attached to a slide in a study, not to a shape.

Routes (each with ``?studyId=<study>``):
  GET    /slide-notes?slide_id=   a page of the slide's notes the user may see, and a tag summary
  POST   /slide-notes             create ``{slide_id, study_id, text?, tags?, visible_to?}``
  PUT    /slide-notes/{id}        update (creator only; ``version`` is an optimistic lock)
  DELETE /slide-notes/{id}        delete (creator only; optional ``version``)

``slide_id`` is the opaque 12-hex slide key, as for annotations.  The body's
study and the stored row's study must be the study the proxy authorized.

Visibility follows annotations: the creator always sees a note;
``visible_to: null`` keeps it private, ``[]`` shows it to everyone who can
read the study, and a list of group names to members of those groups.

A note carries ``text`` (at most ``NOTE_TEXT_MAX_CHARS`` characters), up to
``NOTE_MAX_TAGS`` tags, or both, never neither.  A tag is 1 to
``TAG_MAX_CHARS`` characters of letters, digits, spaces and ``_ - . : / + #``,
starting with a letter or digit; runs of whitespace become one space, and
a tag repeated in another letter case is dropped.  The list's ``tags``
counts, for each tag, all the visible notes that carry it, not only the
page's.

Paging: the list takes ``offset`` (default 0) and ``limit`` (default
``NOTE_PAGE_DEFAULT``, at most ``NOTE_PAGE_MAX``) over the visible notes,
oldest first, and answers ``{slide_id, notes, tags, total, offset, limit,
has_more, next_offset}``; ``total`` counts the visible notes and
``next_offset`` is the offset of the next page (null on the last).  A slide
holds at most ``NOTES_PER_SLIDE_MAX`` notes in a study, from all users; a
note past that is refused with 422.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator

from . import annotations as annotation_store
from .annotation_sql import Transaction
from .request_identity import require_same_study, require_user

router = APIRouter(prefix="/slide-notes", tags=["slide notes"])

NOTE_TEXT_MAX_CHARS = 4000
NOTE_MAX_TAGS = 20
TAG_MAX_CHARS = 40
TAG_PATTERN = re.compile(r"^[^\W_][\w .:/+#-]*$")
# Notes one slide may hold in a study, from all users.
NOTES_PER_SLIDE_MAX = 1000
# Notes per page of the list: by default, and at most.
NOTE_PAGE_DEFAULT = 100
NOTE_PAGE_MAX = 500
# Largest accepted ``offset`` (keeps the bound parameter in range).
PAGE_OFFSET_MAX = 1_000_000

NoteId = Annotated[str, Path(min_length=1, max_length=128)]


def normalize_tags(tags: list[str]) -> list[str]:
    """Validated tags in their given order, without case-insensitive repeats."""
    if len(tags) > NOTE_MAX_TAGS:
        raise ValueError(f"A note may carry at most {NOTE_MAX_TAGS} tags")
    result: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = " ".join(raw.split())
        if not 1 <= len(tag) <= TAG_MAX_CHARS or not TAG_PATTERN.fullmatch(tag):
            raise ValueError(
                f"A tag is 1-{TAG_MAX_CHARS} letters, digits, spaces or _-.:/+#,"
                " starting with a letter or digit"
            )
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    return result


class SlideNoteIn(BaseModel):
    slide_id: str = Field(..., pattern=annotation_store.SLIDE_KEY_REGEX, description="Opaque slide key")
    study_id: str
    text: str = Field(default="", max_length=NOTE_TEXT_MAX_CHARS)
    tags: list[str] = Field(default_factory=list)
    visible_to: list[str] | None = Field(
        default=None,
        description="Group names that may view this note; [] = everyone in the study; null = creator only",
    )

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, value: list[str]) -> list[str]:
        return normalize_tags(value)

    @model_validator(mode="after")
    def _not_empty(self) -> "SlideNoteIn":
        if not self.text and not self.tags:
            raise ValueError("A note needs text or tags")
        return self


class SlideNoteUpdate(BaseModel):
    text: str | None = Field(default=None, max_length=NOTE_TEXT_MAX_CHARS)
    tags: list[str] | None = None
    visible_to: list[str] | None = None
    version: int = Field(..., description="Must match current version (optimistic lock)")

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("tags")
    @classmethod
    def _valid_tags(cls, value: list[str] | None) -> list[str] | None:
        return normalize_tags(value) if value is not None else None


class SlideNoteOut(BaseModel):
    id: str
    slide_id: str
    study_id: str
    text: str
    tags: list[str]
    created_by: str
    visible_to: list[str] | None
    version: int
    created_at: str
    updated_at: str


class TagCount(BaseModel):
    tag: str
    count: int


class SlideNoteList(BaseModel):
    slide_id: str
    notes: list[SlideNoteOut]
    tags: list[TagCount] = Field(
        description="Each tag on all the visible notes (not only this page) with the"
        " number of notes carrying it, most used first"
    )
    total: int = Field(description="Visible notes on the slide")
    offset: int
    limit: int
    has_more: bool = Field(description="Whether notes follow this page")
    next_offset: int | None = Field(description="``offset`` of the next page; null on the last")


def _columns(tx: Transaction) -> str:
    return (
        "n.id, n.slide_id, n.study_id, n.text, n.tags, n.created_by, n.visible_to,"
        f" n.version, {tx.ts('n.created_at')}, {tx.ts('n.updated_at')}"
    )


def _row_to_out(row: dict) -> SlideNoteOut:
    return SlideNoteOut(
        **{
            **row,
            "tags": json.loads(row["tags"]),
            "visible_to": annotation_store._decode_visible_to(row["visible_to"]),
        }
    )


def _tag_summary(notes: list[SlideNoteOut]) -> list[TagCount]:
    counts = Counter(tag for note in notes for tag in note.tags)
    return [
        TagCount(tag=tag, count=count)
        for tag, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold(), item[0]))
    ]


async def _fetch(tx: Transaction, note_id: str, lock: bool = False) -> dict | None:
    return await tx.fetchrow(
        f"SELECT {_columns(tx)} FROM slide_notes n WHERE n.id = $1"
        f"{tx.for_update if lock else ''}",
        note_id,
    )


async def _own_note(tx: Transaction, note_id: str, user: dict, verb: str) -> dict:
    row = await _fetch(tx, note_id, lock=True)
    if row is None:
        raise HTTPException(status_code=404, detail="Slide note not found")
    require_same_study(user, row["study_id"], "the note's study")
    if not annotation_store._can_modify(row, user):
        raise HTTPException(status_code=403, detail=f"Only the creator may {verb} this note")
    return row


async def _require_room(tx: Transaction, slide_id: str, study_id: str) -> None:
    """422 once the slide holds ``NOTES_PER_SLIDE_MAX`` notes in the study.

    On Postgres a transaction-scoped advisory lock on the slide and study
    makes concurrent creates count one at a time; a SQLite write
    transaction already holds the database's write lock.
    """
    if tx.kind == "postgres":
        await tx.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"slide_notes\x1f{study_id}\x1f{slide_id}",
        )
    stored = await tx.fetchrow(
        "SELECT COUNT(*) AS n FROM slide_notes WHERE slide_id = $1 AND study_id = $2",
        slide_id,
        study_id,
    )
    if int(stored["n"]) >= NOTES_PER_SLIDE_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"A slide holds at most {NOTES_PER_SLIDE_MAX} notes",
        )


def _check_version(row: dict, version: int) -> None:
    if row["version"] != version:
        raise HTTPException(
            status_code=409,
            detail=f"Version conflict: expected {row['version']}, got {version}",
        )


@router.get("", response_model=SlideNoteList)
async def list_slide_notes(
    slide_id: str = Query(..., pattern=annotation_store.SLIDE_KEY_REGEX, description="Opaque slide key"),
    user: dict = Depends(require_user),
    offset: Annotated[int, Query(ge=0, le=PAGE_OFFSET_MAX, description="Notes to skip")] = 0,
    limit: Annotated[
        int, Query(ge=1, le=NOTE_PAGE_MAX, description="Notes per page")
    ] = NOTE_PAGE_DEFAULT,
) -> SlideNoteList:
    # Visibility by group is decided here, not in SQL, so the page is cut
    # from all the visible notes; a slide holds at most NOTES_PER_SLIDE_MAX.
    async with annotation_store.storage_transaction() as tx:
        rows = await tx.fetch(
            f"SELECT {_columns(tx)} FROM slide_notes n"
            " WHERE n.slide_id = $1 AND n.study_id = $2"
            " AND (n.created_by = $3 OR n.visible_to IS NOT NULL)"
            f" ORDER BY {tx.created_order('n')}",
            slide_id,
            user["study_id"],
            user["sub"],
        )
    groups = set(user["groups"])
    notes = [
        _row_to_out(row)
        for row in rows
        if annotation_store._is_visible(row, user["sub"], groups)
    ]
    page = notes[offset : offset + limit]
    has_more = offset + len(page) < len(notes)
    return SlideNoteList(
        slide_id=slide_id,
        notes=page,
        tags=_tag_summary(notes),
        total=len(notes),
        offset=offset,
        limit=limit,
        has_more=has_more,
        next_offset=offset + len(page) if has_more else None,
    )


@router.post("", response_model=SlideNoteOut, status_code=status.HTTP_201_CREATED)
async def create_slide_note(
    data: SlideNoteIn,
    user: dict = Depends(require_user),
) -> SlideNoteOut:
    require_same_study(user, data.study_id, "the note's study")
    note_id = str(uuid.uuid4())
    async with annotation_store.storage_transaction(write=True) as tx:
        await _require_room(tx, data.slide_id, data.study_id)
        await tx.execute(
            "INSERT INTO slide_notes"
            " (id, slide_id, study_id, text, tags, created_by, visible_to, version,"
            "  created_at, updated_at)"
            f" VALUES ($1, $2, $3, $4, $5, $6, $7, 1, {tx.now}, {tx.now})",
            note_id,
            data.slide_id,
            data.study_id,
            data.text,
            json.dumps(data.tags),
            user["sub"],
            json.dumps(data.visible_to) if data.visible_to is not None else None,
        )
        row = await _fetch(tx, note_id)
    return _row_to_out(row)


@router.put("/{note_id}", response_model=SlideNoteOut)
async def update_slide_note(
    note_id: NoteId,
    data: SlideNoteUpdate,
    user: dict = Depends(require_user),
) -> SlideNoteOut:
    async with annotation_store.storage_transaction(write=True) as tx:
        existing = await _own_note(tx, note_id, user, "update")
        _check_version(existing, data.version)
        text = data.text if data.text is not None else existing["text"]
        tags = data.tags if data.tags is not None else json.loads(existing["tags"])
        if not text and not tags:
            raise HTTPException(status_code=422, detail="A note needs text or tags")
        if "visible_to" in data.model_fields_set:
            visible: Any = json.dumps(data.visible_to) if data.visible_to is not None else None
        else:
            visible = existing["visible_to"]
        changed = await tx.execute(
            "UPDATE slide_notes SET text = $1, tags = $2, visible_to = $3,"
            f" version = version + 1, updated_at = {tx.now}"
            " WHERE id = $4 AND version = $5",
            text,
            json.dumps(tags),
            visible,
            note_id,
            existing["version"],
        )
        if changed != 1:
            raise HTTPException(status_code=409, detail="Slide note was modified concurrently")
        row = await _fetch(tx, note_id)
    return _row_to_out(row)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_slide_note(
    note_id: NoteId,
    user: dict = Depends(require_user),
    version: Annotated[
        int | None,
        Query(description="When given, must match the current version (optimistic lock)"),
    ] = None,
) -> None:
    async with annotation_store.storage_transaction(write=True) as tx:
        existing = await _own_note(tx, note_id, user, "delete")
        if version is not None:
            _check_version(existing, version)
        await tx.execute("DELETE FROM slide_notes WHERE id = $1", note_id)
