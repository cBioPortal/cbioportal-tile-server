"""Batch update/delete/review, comment threads and slide notes.

Every test runs on SQLite, and on Postgres under ``-m integration``
(the ``annotation_storage`` fixture).
"""

import asyncio
import json

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import annotation_comments, annotation_sql, annotations, slide_notes
from app.annotations import (
    AnnotationBatchDelete,
    AnnotationBatchReview,
    AnnotationBatchUpdate,
    AnnotationBody,
    AnnotationIn,
    AnnotationTarget,
)
from app.annotation_comments import CommentIn
from app.slide_notes import SlideNoteIn, SlideNoteUpdate

SLIDE_KEY = "0123456789ab"
OTHER_SLIDE = "ba9876543210"

pytestmark = pytest.mark.usefixtures("annotation_storage")


def user(subject: str, *, groups: list[str] | None = None, study_id: str = "study-1") -> dict:
    return {"sub": subject, "groups": groups or [], "study_id": study_id}


def annotation_input(
    label: str = "Tumor",
    *,
    study_id: str = "study-1",
    visible_to: list[str] | None = None,
    provenance: dict | None = None,
) -> AnnotationIn:
    return AnnotationIn(
        slide_id=SLIDE_KEY,
        study_id=study_id,
        body=AnnotationBody(label=label, comment="Own note", type="Polygon", provenance=provenance),
        target=AnnotationTarget(selector={"type": "SvgSelector", "value": "<svg><polygon points=\"1,1 4,1 4,4\" /></svg>"}),
        visible_to=visible_to,
    )


async def create(label: str = "Tumor", owner: str = "owner", **kwargs) -> annotations.AnnotationOut:
    study_id = kwargs.get("study_id", "study-1")
    return await annotations.create_annotation(
        annotation_input(label, **kwargs), user(owner, study_id=study_id)
    )


async def stored(owner: str = "owner", **kwargs) -> dict[str, annotations.AnnotationOut]:
    rows = await annotations.list_annotations(SLIDE_KEY, user(owner, **kwargs), include_rejected=True)
    return {row.id: row for row in rows}


def update_items(*items: dict) -> AnnotationBatchUpdate:
    return AnnotationBatchUpdate(items=list(items))


# --- batch update -----------------------------------------------------------


async def test_batch_update_saves_every_item_and_answers_in_request_order():
    first = await create("A", visible_to=["pathology"])
    second = await create("B")
    third = await create("C")

    updated = await annotations.update_annotation_batch(
        update_items(
            {"id": third.id, "version": 1, "body": {"label": "C2"}},
            {"id": first.id, "version": 1, "visible_to": None},
            {"id": second.id, "version": 1, "target": {"selector": {"type": "SvgSelector", "value": "<svg><circle cx=\"1\" cy=\"2\" r=\"0\" /></svg>"}}},
        ),
        user("owner"),
    )

    assert [row.id for row in updated] == [third.id, first.id, second.id]
    assert [row.version for row in updated] == [2, 2, 2]
    assert updated[0].body.label == "C2"
    assert updated[1].visible_to is None and updated[1].body.label == "A"
    assert updated[2].target.selector == {"type": "SvgSelector", "value": "<svg><circle cx=\"1\" cy=\"2\" r=\"0\" /></svg>"}
    assert all(row.updated_at for row in updated)
    rows = await stored()
    assert rows[third.id].body.label == "C2"
    assert rows[first.id].visible_to is None
    assert rows[second.id].version == 2


async def test_batch_update_keeps_visible_to_unless_it_is_sent():
    row = await create(visible_to=["pathology"])
    updated = await annotations.update_annotation_batch(
        update_items({"id": row.id, "version": 1, "body": {"label": "New"}}), user("owner")
    )
    assert updated[0].visible_to == ["pathology"]


@pytest.mark.parametrize("count", [0, 101])
def test_a_batch_has_one_to_a_hundred_items(count):
    items = [{"id": f"a{index}", "version": 1} for index in range(count)]
    with pytest.raises(ValidationError):
        AnnotationBatchUpdate(items=items)
    with pytest.raises(ValidationError):
        AnnotationBatchDelete(items=items)
    with pytest.raises(ValidationError):
        AnnotationBatchReview(items=[{**item, "decision": "accept"} for item in items])


async def test_a_hundred_items_are_accepted():
    created = await annotations.create_annotation_batch(
        annotations.AnnotationBatchIn(annotations=[annotation_input(str(i)) for i in range(100)]),
        user("owner"),
    )
    updated = await annotations.update_annotation_batch(
        AnnotationBatchUpdate(
            items=[{"id": row.id, "version": 1, "body": {"label": "x"}} for row in created]
        ),
        user("owner"),
    )
    assert len(updated) == 100 and {row.version for row in updated} == {2}


async def test_an_id_may_appear_once_in_a_batch():
    row = await create()
    with pytest.raises(HTTPException) as error:
        await annotations.update_annotation_batch(
            update_items({"id": row.id, "version": 1}, {"id": row.id, "version": 1}), user("owner")
        )
    assert error.value.status_code == 422
    assert error.value.detail[0]["loc"] == ["body", "items", 1, "id"]


async def test_a_missing_annotation_fails_the_batch_naming_its_index():
    row = await create()
    with pytest.raises(HTTPException) as error:
        await annotations.update_annotation_batch(
            update_items(
                {"id": row.id, "version": 1, "body": {"label": "changed"}},
                {"id": "missing", "version": 1},
            ),
            user("owner"),
        )
    assert error.value.status_code == 404
    assert error.value.detail["index"] == 1
    assert error.value.detail["id"] == "missing"
    assert error.value.detail["loc"] == ["body", "items", 1]
    assert (await stored())[row.id].body.label == "Tumor"


async def test_only_the_creator_may_update_in_a_batch():
    mine = await create()
    theirs = await create(owner="other", visible_to=[])
    with pytest.raises(HTTPException) as error:
        await annotations.update_annotation_batch(
            update_items(
                {"id": mine.id, "version": 1, "body": {"label": "changed"}},
                {"id": theirs.id, "version": 1, "body": {"label": "changed"}},
            ),
            user("owner"),
        )
    assert error.value.status_code == 403
    assert error.value.detail["index"] == 1
    assert "creator" in error.value.detail["msg"]
    assert (await stored())[mine.id].version == 1


async def test_a_batch_stays_in_the_authorized_study():
    here = await create()
    there = await create(study_id="study-2")
    with pytest.raises(HTTPException) as error:
        await annotations.update_annotation_batch(
            update_items({"id": here.id, "version": 1}, {"id": there.id, "version": 1}),
            user("owner"),
        )
    assert error.value.status_code == 403
    assert error.value.detail["index"] == 1
    assert "study" in error.value.detail["msg"]


async def test_stale_versions_are_reported_together_and_nothing_is_saved():
    rows = [await create(str(i)) for i in range(3)]
    with pytest.raises(HTTPException) as error:
        await annotations.update_annotation_batch(
            update_items(
                {"id": rows[0].id, "version": 4, "body": {"label": "x"}},
                {"id": rows[1].id, "version": 1, "body": {"label": "x"}},
                {"id": rows[2].id, "version": 2, "body": {"label": "x"}},
            ),
            user("owner"),
        )
    assert error.value.status_code == 409
    conflicts = error.value.detail["conflicts"]
    assert [(c["index"], c["id"], c["version"], c["current_version"]) for c in conflicts] == [
        (0, rows[0].id, 4, 1),
        (2, rows[2].id, 2, 1),
    ]
    assert "item 0" in error.value.detail["msg"] and "item 2" in error.value.detail["msg"]
    assert {row.body.label for row in (await stored()).values()} == {"0", "1", "2"}


async def test_batch_update_applies_the_provenance_rules(monkeypatch):
    human = await create()
    with pytest.raises(HTTPException) as error:
        await annotations.update_annotation_batch(
            update_items(
                {"id": human.id, "version": 1, "body": {"label": "A", "provenance": {"source": "model"}}}
            ),
            user("owner"),
        )
    assert error.value.status_code == 422
    assert error.value.detail[0]["loc"] == ["body", "items", 0]
    assert "trusted writer" in error.value.detail[0]["msg"]

    machine = await create(owner="loader", visible_to=[], provenance={"source": "model"})
    reviewed = await annotations.review_annotation(
        machine.id, annotations.AnnotationReview(decision="accept", version=1), user("reviewer")
    )
    # The creator, no longer a trusted writer, cannot forge the review.
    monkeypatch.setattr(annotations.settings, "annotation_trusted_writers", [])
    updated = await annotations.update_annotation_batch(
        update_items(
            {
                "id": machine.id,
                "version": 2,
                "body": {"label": "B", "provenance": {"source": "model", "review_status": "rejected"}},
            }
        ),
        user("loader"),
    )
    assert updated[0].body.provenance["review_status"] == "accepted"
    assert updated[0].body.provenance["reviewed_at"] == reviewed.body.provenance["reviewed_at"]


async def test_a_storage_failure_mid_batch_saves_nothing(monkeypatch):
    rows = [await create(str(i)) for i in range(3)]
    real_execute = annotation_sql.Transaction.execute
    calls = 0

    async def failing_execute(self, statement, *args):
        nonlocal calls
        if statement.startswith("UPDATE annotations"):
            calls += 1
            if calls == 2:
                raise RuntimeError("storage failed")
        return await real_execute(self, statement, *args)

    monkeypatch.setattr(annotation_sql.Transaction, "execute", failing_execute)
    with pytest.raises(RuntimeError):
        await annotations.update_annotation_batch(
            AnnotationBatchUpdate(
                items=[{"id": row.id, "version": 1, "body": {"label": "x"}} for row in rows]
            ),
            user("owner"),
        )
    monkeypatch.setattr(annotation_sql.Transaction, "execute", real_execute)
    assert {row.version for row in (await stored()).values()} == {1}


async def test_concurrent_batches_take_one_version():
    row = await create()
    results = await asyncio.gather(
        *(
            annotations.update_annotation_batch(
                update_items({"id": row.id, "version": 1, "body": {"label": label}}), user("owner")
            )
            for label in ("A", "B")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, list) for result in results) == 1
    assert [r.status_code for r in results if isinstance(r, HTTPException)] == [409]


# --- batch delete -----------------------------------------------------------


async def test_batch_delete_hides_every_item_and_keeps_its_thread():
    rows = [await create(str(i), visible_to=[]) for i in range(3)]
    await annotation_comments.create_comment(rows[0].id, CommentIn(body="hi"), user("other"))

    deleted = await annotations.delete_annotation_batch(
        AnnotationBatchDelete(items=[{"id": rows[2].id, "version": 1}, {"id": rows[0].id, "version": 1}]),
        user("owner"),
    )

    assert [row.id for row in deleted] == [rows[2].id, rows[0].id]
    assert deleted[1].comment_count == 1
    assert [row.version for row in deleted] == [1, 1]
    assert list(await stored()) == [rows[1].id]
    async with annotations.storage_transaction() as tx:
        left = await tx.fetch("SELECT id FROM annotation_comments WHERE annotation_id = $1", rows[0].id)
    assert len(left) == 1


async def test_batch_delete_is_creator_only_and_versioned():
    mine = await create()
    theirs = await create(owner="other", visible_to=[])
    with pytest.raises(HTTPException) as forbidden:
        await annotations.delete_annotation_batch(
            AnnotationBatchDelete(items=[{"id": mine.id, "version": 1}, {"id": theirs.id, "version": 1}]),
            user("owner"),
        )
    assert forbidden.value.status_code == 403 and forbidden.value.detail["index"] == 1

    with pytest.raises(HTTPException) as stale:
        await annotations.delete_annotation_batch(
            AnnotationBatchDelete(items=[{"id": mine.id, "version": 3}]), user("owner")
        )
    assert stale.value.status_code == 409
    assert stale.value.detail["conflicts"][0]["current_version"] == 1
    assert mine.id in await stored()


# --- batch review -----------------------------------------------------------


async def machine(label: str = "AI", visible_to: list[str] | None = None) -> annotations.AnnotationOut:
    return await create(
        label, owner="loader", visible_to=[] if visible_to is None else visible_to,
        provenance={"source": "precomputed"},
    )


async def test_any_reader_may_review_a_batch():
    rows = [await machine(str(i)) for i in range(3)]
    reviewed = await annotations.review_annotation_batch(
        AnnotationBatchReview(
            items=[
                {"id": rows[0].id, "version": 1, "decision": "accept"},
                {"id": rows[1].id, "version": 1, "decision": "reject"},
            ]
        ),
        user("reviewer"),
    )
    assert [row.body.provenance["review_status"] for row in reviewed] == ["accepted", "rejected"]
    assert {row.body.provenance["reviewed_by"] for row in reviewed} == {"reviewer"}
    assert [row.version for row in reviewed] == [2, 2]
    listed = await annotations.list_annotations(SLIDE_KEY, user("reviewer"))
    assert {row.id for row in listed} == {rows[0].id, rows[2].id}


async def test_batch_review_applies_the_review_rules():
    ai = await machine()
    human = await create(visible_to=[])
    hidden = await machine(visible_to=None)
    hidden = await annotations.update_annotation(
        hidden.id, annotations.AnnotationUpdate(visible_to=None, version=1), user("loader")
    )

    def batch(*rows):
        return AnnotationBatchReview(
            items=[{"id": row.id, "version": row.version, "decision": "accept"} for row in rows]
        )

    with pytest.raises(HTTPException) as not_machine:
        await annotations.review_annotation_batch(batch(ai, human), user("reviewer"))
    assert not_machine.value.status_code == 403 and not_machine.value.detail["index"] == 1

    with pytest.raises(HTTPException) as invisible:
        await annotations.review_annotation_batch(batch(ai, hidden), user("reviewer"))
    assert invisible.value.status_code == 404 and invisible.value.detail["index"] == 1

    with pytest.raises(HTTPException) as other_study:
        await annotations.review_annotation_batch(batch(ai), user("reviewer", study_id="study-2"))
    assert other_study.value.status_code == 403

    assert (await stored("loader"))[ai.id].version == 1


# --- comment threads --------------------------------------------------------


async def test_a_thread_is_readable_and_writable_by_anyone_who_sees_the_annotation():
    row = await create(visible_to=[])
    first = await annotation_comments.create_comment(row.id, CommentIn(body="  Looks solid  "), user("owner"))
    second = await annotation_comments.create_comment(row.id, CommentIn(body="Agree"), user("reviewer"))

    assert first.body == "Looks solid"
    assert first.author == "owner" and second.author == "reviewer"
    assert first.study_id == "study-1" and first.annotation_id == row.id
    assert first.created_at.endswith("Z") and "T" in first.created_at
    thread = await annotation_comments.list_comments(row.id, user("third"))
    assert [comment.id for comment in thread.comments] == [first.id, second.id]
    assert (thread.total, thread.has_more, thread.next_offset) == (2, False, None)

    listed = await annotations.list_annotations(SLIDE_KEY, user("third"))
    assert listed[0].comment_count == 2
    # The annotation's own note is separate from its thread.
    assert listed[0].body.comment == "Own note"
    assert listed[0].version == 1


async def test_only_the_author_edits_or_deletes_a_comment():
    row = await create(visible_to=[])
    comment = await annotation_comments.create_comment(row.id, CommentIn(body="v1"), user("reviewer"))

    for call in (
        annotation_comments.update_comment(row.id, comment.id, CommentIn(body="x"), user("owner")),
        annotation_comments.delete_comment(row.id, comment.id, user("owner")),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 403

    edited = await annotation_comments.update_comment(
        row.id, comment.id, CommentIn(body="v2"), user("reviewer")
    )
    assert edited.body == "v2" and edited.created_at == comment.created_at

    await annotation_comments.delete_comment(row.id, comment.id, user("reviewer"))
    assert (await annotation_comments.list_comments(row.id, user("owner"))).comments == []
    assert (await stored())[row.id].comment_count == 0
    with pytest.raises(HTTPException) as gone:
        await annotation_comments.update_comment(row.id, comment.id, CommentIn(body="v3"), user("reviewer"))
    assert gone.value.status_code == 404
    # Soft delete: the row is kept, flagged.
    async with annotations.storage_transaction() as tx:
        kept = await tx.fetchrow("SELECT body, deleted FROM annotation_comments WHERE id = $1", comment.id)
    assert kept["body"] == "v2" and bool(kept["deleted"]) is True


async def test_a_comment_must_be_on_the_named_annotation():
    one = await create(visible_to=[])
    two = await create(visible_to=[])
    comment = await annotation_comments.create_comment(one.id, CommentIn(body="hi"), user("owner"))
    with pytest.raises(HTTPException) as error:
        await annotation_comments.update_comment(two.id, comment.id, CommentIn(body="x"), user("owner"))
    assert error.value.status_code == 404


async def test_threads_follow_the_annotations_study_and_visibility():
    private = await create()
    public = await create(visible_to=[])
    group = await create(visible_to=["pathology"])
    await annotation_comments.create_comment(private.id, CommentIn(body="note to self"), user("owner"))

    for call in (
        annotation_comments.list_comments(private.id, user("other")),
        annotation_comments.create_comment(private.id, CommentIn(body="x"), user("other")),
        annotation_comments.list_comments("missing", user("owner")),
        annotation_comments.create_comment(group.id, CommentIn(body="x"), user("other")),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 404

    member = user("member", groups=["pathology"])
    assert await annotation_comments.create_comment(group.id, CommentIn(body="ok"), member)

    with pytest.raises(HTTPException) as other_study:
        await annotation_comments.list_comments(public.id, user("owner", study_id="study-2"))
    assert other_study.value.status_code == 403

    # An author who can no longer see the annotation cannot edit there either.
    comment = await annotation_comments.create_comment(public.id, CommentIn(body="x"), user("other"))
    await annotations.update_annotation(
        public.id, annotations.AnnotationUpdate(visible_to=None, version=1), user("owner")
    )
    with pytest.raises(HTTPException) as hidden:
        await annotation_comments.update_comment(public.id, comment.id, CommentIn(body="y"), user("other"))
    assert hidden.value.status_code == 404


async def test_deleting_an_annotation_keeps_its_thread():
    """Soft delete keeps the thread for a restore (see test_annotation_undo)."""
    row = await create(visible_to=[])
    await annotation_comments.create_comment(row.id, CommentIn(body="hi"), user("other"))
    await annotations.delete_annotation(row.id, user("owner"))
    async with annotations.storage_transaction() as tx:
        assert len(await tx.fetch("SELECT id FROM annotation_comments")) == 1


def test_a_comment_body_is_capped_and_not_blank():
    assert CommentIn(body="x" * annotation_comments.COMMENT_MAX_CHARS)
    for body in ("", "   ", "x" * (annotation_comments.COMMENT_MAX_CHARS + 1)):
        with pytest.raises(ValidationError):
            CommentIn(body=body)


async def test_updates_and_reviews_report_the_comment_count():
    row = await create(visible_to=[])
    await annotation_comments.create_comment(row.id, CommentIn(body="hi"), user("other"))
    updated = await annotations.update_annotation(
        row.id, annotations.AnnotationUpdate(body=AnnotationBody(label="x"), version=1), user("owner")
    )
    assert updated.comment_count == 1
    batch = await annotations.update_annotation_batch(
        update_items({"id": row.id, "version": 2}), user("owner")
    )
    assert batch[0].comment_count == 1


# --- slide notes ------------------------------------------------------------


def note_input(text: str = "Margin involved", **kwargs) -> SlideNoteIn:
    return SlideNoteIn(slide_id=kwargs.pop("slide_id", SLIDE_KEY), study_id=kwargs.pop("study_id", "study-1"), text=text, **kwargs)


async def test_slide_notes_round_trip():
    owner = user("owner")
    note = await slide_notes.create_slide_note(
        note_input("  Margin involved ", tags=["LUAD", "needs  review"], visible_to=[]), owner
    )
    assert note.text == "Margin involved"
    assert note.tags == ["LUAD", "needs review"]
    assert note.version == 1 and note.created_by == "owner"
    assert note.created_at.endswith("Z") and note.updated_at.endswith("Z")

    listed = await slide_notes.list_slide_notes(SLIDE_KEY, user("reader"))
    assert listed.slide_id == SLIDE_KEY
    assert [item.id for item in listed.notes] == [note.id]

    updated = await slide_notes.update_slide_note(
        note.id, SlideNoteUpdate(tags=["LUAD"], version=1), owner
    )
    assert updated.version == 2 and updated.tags == ["LUAD"] and updated.text == "Margin involved"
    assert updated.visible_to == []

    with pytest.raises(HTTPException) as stale:
        await slide_notes.delete_slide_note(note.id, owner, version=1)
    assert stale.value.status_code == 409
    await slide_notes.delete_slide_note(note.id, owner, version=2)
    assert (await slide_notes.list_slide_notes(SLIDE_KEY, owner)).notes == []


async def test_slide_note_visibility_matches_annotations():
    owner = user("owner")
    private = await slide_notes.create_slide_note(note_input("private"), owner)
    group = await slide_notes.create_slide_note(note_input("group", visible_to=["pathology"]), owner)
    public = await slide_notes.create_slide_note(note_input("public", visible_to=[]), owner)
    await slide_notes.create_slide_note(note_input("elsewhere", slide_id=OTHER_SLIDE, visible_to=[]), owner)
    await slide_notes.create_slide_note(note_input("other study", study_id="study-2", visible_to=[]), user("owner", study_id="study-2"))

    def ids(listing):
        return {note.id for note in listing.notes}

    assert ids(await slide_notes.list_slide_notes(SLIDE_KEY, user("x"))) == {public.id}
    assert ids(await slide_notes.list_slide_notes(SLIDE_KEY, user("x", groups=["pathology"]))) == {group.id, public.id}
    assert ids(await slide_notes.list_slide_notes(SLIDE_KEY, owner)) == {private.id, group.id, public.id}


async def test_the_tag_summary_counts_visible_notes():
    owner = user("owner")
    await slide_notes.create_slide_note(note_input("a", tags=["LUAD", "QC"], visible_to=[]), owner)
    await slide_notes.create_slide_note(note_input("b", tags=["luad"], visible_to=[]), user("second"))
    await slide_notes.create_slide_note(note_input("c", tags=["QC"], visible_to=[]), owner)
    await slide_notes.create_slide_note(note_input("d", tags=["secret"]), owner)

    listing = await slide_notes.list_slide_notes(SLIDE_KEY, user("reader"))
    assert [(t.tag, t.count) for t in listing.tags] == [("QC", 2), ("LUAD", 1), ("luad", 1)]


async def test_slide_notes_are_creator_only_and_study_scoped():
    note = await slide_notes.create_slide_note(note_input(visible_to=[]), user("owner"))
    for call in (
        slide_notes.update_slide_note(note.id, SlideNoteUpdate(text="x", version=1), user("other")),
        slide_notes.delete_slide_note(note.id, user("other")),
        slide_notes.update_slide_note(note.id, SlideNoteUpdate(text="x", version=1), user("owner", study_id="study-2")),
        slide_notes.create_slide_note(note_input(study_id="study-2"), user("owner")),
    ):
        with pytest.raises(HTTPException) as error:
            await call
        assert error.value.status_code == 403
    with pytest.raises(HTTPException) as missing:
        await slide_notes.update_slide_note("missing", SlideNoteUpdate(text="x", version=1), user("owner"))
    assert missing.value.status_code == 404


async def test_slide_note_updates_are_versioned():
    owner = user("owner")
    note = await slide_notes.create_slide_note(note_input(tags=["a"]), owner)
    with pytest.raises(HTTPException) as stale:
        await slide_notes.update_slide_note(note.id, SlideNoteUpdate(text="x", version=2), owner)
    assert stale.value.status_code == 409

    results = await asyncio.gather(
        slide_notes.update_slide_note(note.id, SlideNoteUpdate(text="one", version=1), owner),
        slide_notes.update_slide_note(note.id, SlideNoteUpdate(text="two", version=1), owner),
        return_exceptions=True,
    )
    assert [r.status_code for r in results if isinstance(r, HTTPException)] == [409]

    with pytest.raises(HTTPException) as emptied:
        await slide_notes.update_slide_note(note.id, SlideNoteUpdate(text="", tags=[], version=2), owner)
    assert emptied.value.status_code == 422
    private = await slide_notes.update_slide_note(note.id, SlideNoteUpdate(visible_to=None, version=2), owner)
    assert private.visible_to is None and private.tags == ["a"]


@pytest.mark.parametrize(
    "tags",
    [
        [""],
        ["   "],
        ["-leading"],
        ["x" * (slide_notes.TAG_MAX_CHARS + 1)],
        ["semi;colon"],
        ["bell\x07"],
        [f"t{i}" for i in range(slide_notes.NOTE_MAX_TAGS + 1)],
    ],
)
def test_invalid_tags_are_refused(tags):
    with pytest.raises(ValidationError):
        note_input(tags=tags)
    with pytest.raises(ValidationError):
        SlideNoteUpdate(tags=tags, version=1)


def test_tags_are_normalized_and_deduplicated():
    note = note_input(tags=["KRAS  G12C", "kras g12c", "PD-L1", "grade:2", "Ki67/high", "Ünïcode"])
    assert note.tags == ["KRAS G12C", "PD-L1", "grade:2", "Ki67/high", "Ünïcode"]
    assert note_input(tags=[f"t{i}" for i in range(slide_notes.NOTE_MAX_TAGS)])


def test_a_note_needs_text_or_tags_and_a_slide_key():
    with pytest.raises(ValidationError):
        note_input("  ")
    assert note_input("", tags=["QC"]).text == ""
    with pytest.raises(ValidationError):
        note_input("x" * (slide_notes.NOTE_TEXT_MAX_CHARS + 1))
    with pytest.raises(ValidationError):
        note_input(slide_id="470775")


async def test_slide_note_tags_are_stored_as_json():
    note = await slide_notes.create_slide_note(note_input(tags=["a", "b"]), user("owner"))
    async with annotations.storage_transaction() as tx:
        row = await tx.fetchrow("SELECT tags FROM slide_notes WHERE id = $1", note.id)
    assert json.loads(row["tags"]) == ["a", "b"]


# --- paging and caps --------------------------------------------------------


async def test_a_thread_is_listed_a_page_at_a_time():
    row = await create(visible_to=[])
    made = [
        await annotation_comments.create_comment(row.id, CommentIn(body=f"c{i}"), user("owner"))
        for i in range(5)
    ]
    await annotation_comments.delete_comment(row.id, made[1].id, user("owner"))
    live = [made[0].id, *[c.id for c in made[2:]]]

    first = await annotation_comments.list_comments(row.id, user("reader"), offset=0, limit=2)
    assert [c.id for c in first.comments] == live[:2]
    assert (first.total, first.offset, first.limit) == (4, 0, 2)
    assert (first.has_more, first.next_offset) == (True, 2)
    last = await annotation_comments.list_comments(row.id, user("reader"), offset=2, limit=2)
    assert [c.id for c in last.comments] == live[2:]
    assert (last.has_more, last.next_offset) == (False, None)
    past = await annotation_comments.list_comments(row.id, user("reader"), offset=10, limit=2)
    assert past.comments == [] and past.total == 4 and past.next_offset is None

    default = await annotation_comments.list_comments(row.id, user("reader"))
    assert default.limit == annotation_comments.COMMENT_PAGE_DEFAULT
    assert [c.id for c in default.comments] == live


async def test_an_annotation_holds_a_bounded_number_of_comments(monkeypatch):
    monkeypatch.setattr(annotation_comments, "COMMENTS_PER_ANNOTATION_MAX", 3)
    row = await create(visible_to=[])
    other = await create("Other", visible_to=[])
    made = [
        await annotation_comments.create_comment(row.id, CommentIn(body=f"c{i}"), user("owner"))
        for i in range(3)
    ]
    # Deleted comments still count, so delete-and-repost cannot grow a thread.
    await annotation_comments.delete_comment(row.id, made[0].id, user("owner"))
    with pytest.raises(HTTPException) as full:
        await annotation_comments.create_comment(row.id, CommentIn(body="more"), user("reader"))
    assert full.value.status_code == 422
    assert "at most 3 comments" in full.value.detail
    # The cap is per annotation.
    assert await annotation_comments.create_comment(other.id, CommentIn(body="ok"), user("owner"))


async def test_concurrent_comments_stop_at_the_cap(monkeypatch):
    monkeypatch.setattr(annotation_comments, "COMMENTS_PER_ANNOTATION_MAX", 3)
    row = await create(visible_to=[])
    results = await asyncio.gather(
        *(
            annotation_comments.create_comment(row.id, CommentIn(body=f"c{i}"), user("owner"))
            for i in range(6)
        ),
        return_exceptions=True,
    )
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(refused) == 3 and {r.status_code for r in refused} == {422}
    assert (await annotation_comments.list_comments(row.id, user("owner"))).total == 3


async def test_slide_notes_are_listed_a_page_at_a_time_with_a_whole_slide_tag_summary():
    owner = user("owner")
    made = [
        await slide_notes.create_slide_note(
            note_input(f"n{i}", tags=["QC"] if i % 2 else ["LUAD"], visible_to=[]), owner
        )
        for i in range(5)
    ]
    # Hidden from the reader: neither listed nor counted.
    await slide_notes.create_slide_note(note_input("secret", tags=["QC"]), owner)

    first = await slide_notes.list_slide_notes(SLIDE_KEY, user("reader"), offset=0, limit=2)
    assert [n.id for n in first.notes] == [n.id for n in made[:2]]
    assert (first.total, first.offset, first.limit, first.has_more, first.next_offset) == (5, 0, 2, True, 2)
    assert [(t.tag, t.count) for t in first.tags] == [("LUAD", 3), ("QC", 2)]
    last = await slide_notes.list_slide_notes(SLIDE_KEY, user("reader"), offset=4, limit=2)
    assert [n.id for n in last.notes] == [made[4].id]
    assert (last.has_more, last.next_offset) == (False, None)
    assert [(t.tag, t.count) for t in last.tags] == [("LUAD", 3), ("QC", 2)]

    mine = await slide_notes.list_slide_notes(SLIDE_KEY, owner)
    assert mine.total == 6 and mine.limit == slide_notes.NOTE_PAGE_DEFAULT
    assert [(t.tag, t.count) for t in mine.tags] == [("LUAD", 3), ("QC", 3)]


async def test_a_slide_holds_a_bounded_number_of_notes(monkeypatch):
    monkeypatch.setattr(slide_notes, "NOTES_PER_SLIDE_MAX", 3)
    for i in range(2):
        await slide_notes.create_slide_note(note_input(f"n{i}"), user("owner"))
    await slide_notes.create_slide_note(note_input("n2"), user("second"))
    with pytest.raises(HTTPException) as full:
        await slide_notes.create_slide_note(note_input("n3"), user("third"))
    assert full.value.status_code == 422
    assert "at most 3 notes" in full.value.detail
    # Per slide and study.
    assert await slide_notes.create_slide_note(note_input("other", slide_id=OTHER_SLIDE), user("owner"))
    assert await slide_notes.create_slide_note(note_input("s2", study_id="study-2"), user("owner", study_id="study-2"))


async def test_concurrent_notes_stop_at_the_cap(monkeypatch):
    monkeypatch.setattr(slide_notes, "NOTES_PER_SLIDE_MAX", 3)
    results = await asyncio.gather(
        *(slide_notes.create_slide_note(note_input(f"n{i}"), user("owner")) for i in range(6)),
        return_exceptions=True,
    )
    refused = [r for r in results if isinstance(r, HTTPException)]
    assert len(refused) == 3 and {r.status_code for r in refused} == {422}
    assert (await slide_notes.list_slide_notes(SLIDE_KEY, user("owner"))).total == 3


# --- selectors on write -----------------------------------------------------

SCRIPT_TARGET = {"selector": {"type": "SvgSelector", "value": "<svg><script>alert(1)</script></svg>"}}


async def test_a_stored_selector_is_not_rechecked():
    """A row written before the check stays readable, and editable without its target."""
    row = await create(visible_to=[])
    async with annotations.storage_transaction(write=True) as tx:
        await tx.execute(
            "UPDATE annotations SET target = $1 WHERE id = $2", json.dumps(SCRIPT_TARGET), row.id
        )
    listed = await stored()
    assert listed[row.id].target.selector == SCRIPT_TARGET["selector"]
    updated = await annotations.update_annotation(
        row.id, annotations.AnnotationUpdate(body=AnnotationBody(label="x"), version=1), user("owner")
    )
    assert updated.target.selector == SCRIPT_TARGET["selector"]
    batch = await annotations.update_annotation_batch(
        update_items({"id": row.id, "version": 2, "body": {"label": "y"}}), user("owner")
    )
    assert batch[0].version == 3
    with pytest.raises(ValidationError):
        update_items({"id": row.id, "version": 3, "target": SCRIPT_TARGET})


# --- lost writes and orphans -----------------------------------------------


async def test_a_single_update_of_a_row_deleted_meanwhile_is_404(monkeypatch):
    row = await create()
    stale = await annotations._get_existing(row.id)
    await annotations.delete_annotation(row.id, user("owner"))
    real = annotations._get_existing
    reads = iter([stale])
    monkeypatch.setattr(
        annotations, "_get_existing", lambda annotation_id: _first_then(reads, real, annotation_id)
    )
    with pytest.raises(HTTPException) as gone:
        await annotations.update_annotation(
            row.id, annotations.AnnotationUpdate(body=AnnotationBody(label="x"), version=1), user("owner")
        )
    assert gone.value.status_code == 404


async def test_a_single_update_that_lost_a_race_is_still_409(monkeypatch):
    row = await create()
    stale = await annotations._get_existing(row.id)
    await annotations.update_annotation(
        row.id, annotations.AnnotationUpdate(body=AnnotationBody(label="first"), version=1), user("owner")
    )
    real = annotations._get_existing
    reads = iter([stale])
    monkeypatch.setattr(
        annotations, "_get_existing", lambda annotation_id: _first_then(reads, real, annotation_id)
    )
    with pytest.raises(HTTPException) as lost:
        await annotations.update_annotation(
            row.id, annotations.AnnotationUpdate(body=AnnotationBody(label="x"), version=1), user("owner")
        )
    assert lost.value.status_code == 409


async def test_a_single_delete_of_a_row_deleted_meanwhile_is_404(monkeypatch):
    row = await create()
    stale = await annotations._get_existing(row.id)
    await annotations.delete_annotation(row.id, user("owner"))

    async def still_there(annotation_id):
        return stale

    monkeypatch.setattr(annotations, "_get_existing", still_there)
    with pytest.raises(HTTPException) as gone:
        await annotations.delete_annotation(row.id, user("owner"))
    assert gone.value.status_code == 404


async def _first_then(reads, real, annotation_id):
    for row in reads:
        return row
    return await real(annotation_id)


async def test_orphaned_comments_are_swept():
    kept = await create(visible_to=[])
    await annotation_comments.create_comment(kept.id, CommentIn(body="kept"), user("owner"))
    async with annotations.storage_transaction(write=True) as tx:
        await tx.execute(
            "INSERT INTO annotation_comments (id, annotation_id, study_id, author, body,"
            " created_at, updated_at, deleted)"
            f" VALUES ('orphan', 'gone', 'study-1', 'x', 'y', {tx.now}, {tx.now}, FALSE)"
        )
    assert await annotations.sweep_orphan_comments() == 1
    assert await annotations.sweep_orphan_comments() == 0
    async with annotations.storage_transaction() as tx:
        rows = await tx.fetch("SELECT annotation_id FROM annotation_comments")
    assert [r["annotation_id"] for r in rows] == [kept.id]
