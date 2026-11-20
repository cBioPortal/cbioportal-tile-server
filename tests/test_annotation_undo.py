"""Undoing a review, soft delete, restore and the retention purge.

Every test runs on SQLite, and on Postgres under ``-m integration``
(the ``annotation_storage`` fixture).
"""

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import agent, annotation_comments, annotations, slide_notes
from app.annotation_comments import CommentIn
from app.annotations import (
    AnnotationBatchDelete,
    AnnotationBatchRestore,
    AnnotationBatchReview,
    AnnotationBatchUpdate,
    AnnotationBody,
    AnnotationIn,
    AnnotationRestore,
    AnnotationReview,
    AnnotationTarget,
    AnnotationUpdate,
)
from app.slide_notes import SlideNoteIn

SLIDE_KEY = "0123456789ab"

pytestmark = pytest.mark.usefixtures("annotation_storage")


def user(subject: str, *, study_id: str = "study-1") -> dict:
    return {"sub": subject, "groups": [], "study_id": study_id}


async def create(
    label: str = "Tumor",
    owner: str = "owner",
    *,
    visible_to: list[str] | None = None,
    provenance: dict | None = None,
) -> annotations.AnnotationOut:
    return await annotations.create_annotation(
        AnnotationIn(
            slide_id=SLIDE_KEY,
            study_id="study-1",
            body=AnnotationBody(label=label, type="Polygon", provenance=provenance),
            target=AnnotationTarget(
                selector={"type": "SvgSelector", "value": '<svg><polygon points="1,1 4,1 4,4" /></svg>'}
            ),
            visible_to=visible_to,
        ),
        user(owner),
    )


async def machine(label: str = "AI", visible_to: list[str] | None = None) -> annotations.AnnotationOut:
    return await create(
        label, "loader", visible_to=[] if visible_to is None else visible_to,
        provenance={"source": "precomputed", "model": "m1"},
    )


async def listed(viewer: str = "owner", include_rejected: bool = False) -> dict[str, annotations.AnnotationOut]:
    rows = await annotations.list_annotations(SLIDE_KEY, user(viewer), include_rejected=include_rejected)
    return {row.id: row for row in rows}


async def review(row_id: str, decision: str, version: int, reviewer: str = "reviewer"):
    return await annotations.review_annotation(
        row_id, AnnotationReview(decision=decision, version=version), user(reviewer)
    )


async def status_of(coroutine) -> int:
    with pytest.raises(HTTPException) as error:
        await coroutine
    return error.value.status_code


async def age_deletion(annotation_id: str, days: int) -> None:
    """Pretend ``annotation_id`` was deleted ``days`` days ago."""
    async with annotations.storage_transaction(write=True) as tx:
        if tx.kind == "postgres":
            ago = "timezone('utc', now()) - make_interval(days => $2)"
        else:
            ago = "datetime('now', '-' || $2 || ' days')"
        await tx.execute(f"UPDATE annotations SET deleted_at = {ago} WHERE id = $1", annotation_id, days)


async def raw_rows(table: str, column: str, value: str) -> list[dict]:
    async with annotations.storage_transaction() as tx:
        return await tx.fetch(f"SELECT * FROM {table} WHERE {column} = $1", value)


# --- resetting a review -----------------------------------------------------


@pytest.mark.parametrize("decision", ["accept", "reject"])
async def test_the_reviewer_may_reset_a_review(decision):
    row = await machine()
    await review(row.id, decision, 1)

    reset = await review(row.id, "reset", 2)

    assert reset.version == 3
    assert reset.body.provenance == {"source": "precomputed", "model": "m1"}
    assert reset.body.label == "AI"
    stored = (await listed("reviewer"))[row.id]
    assert stored.version == 3 and stored.body.provenance == reset.body.provenance


async def test_a_reset_rejected_annotation_is_listed_again():
    row = await machine()
    await review(row.id, "reject", 1)
    assert row.id not in await listed("reviewer")
    await review(row.id, "reset", 2)
    assert row.id in await listed("reviewer")
    assert row.id in await listed("other")


async def test_only_the_reviewer_may_reset():
    row = await machine()
    await review(row.id, "accept", 1)
    # Neither another reader nor the annotation's creator.
    assert await status_of(review(row.id, "reset", 2, reviewer="other")) == 403
    assert await status_of(review(row.id, "reset", 2, reviewer="loader")) == 403
    # After a new review, its reviewer may reset it, and the earlier one may not.
    await review(row.id, "accept", 2, reviewer="other")
    assert await status_of(review(row.id, "reset", 3)) == 403
    assert (await review(row.id, "reset", 3, reviewer="other")).version == 4


async def test_an_unreviewed_annotation_has_nothing_to_reset():
    row = await machine()
    assert await status_of(review(row.id, "reset", 1)) == 422
    await review(row.id, "accept", 1)
    await review(row.id, "reset", 2)
    assert await status_of(review(row.id, "reset", 3)) == 422


async def test_a_review_without_a_reviewer_cannot_be_reset():
    # A trusted writer may store review fields; with no reviewed_by, no one may reset.
    row = await create("AI", "loader", visible_to=[], provenance={"source": "model", "review_status": "accepted"})
    assert await status_of(review(row.id, "reset", 1)) == 403
    assert await status_of(review(row.id, "reset", 1, reviewer="loader")) == 403


async def test_reset_keeps_the_review_rules_and_the_version_check():
    row = await machine()
    await review(row.id, "accept", 1)
    assert await status_of(review(row.id, "reset", 1)) == 409
    assert await status_of(
        annotations.review_annotation(
            row.id, AnnotationReview(decision="reset", version=2), user("reviewer", study_id="study-2")
        )
    ) == 403
    human = await create(visible_to=[])
    assert await status_of(review(human.id, "reset", 1)) == 403
    hidden = await machine(visible_to=["secret"])
    assert await status_of(review(hidden.id, "reset", 1)) == 404
    assert await status_of(review("missing", "reset", 1)) == 404
    assert (await listed("reviewer"))[row.id].version == 2


async def test_concurrent_resets_take_one_version():
    row = await machine()
    await review(row.id, "accept", 1)
    results = await asyncio.gather(
        review(row.id, "reset", 2), review(row.id, "reset", 2), return_exceptions=True
    )
    assert sum(isinstance(r, annotations.AnnotationOut) for r in results) == 1
    assert [r.status_code for r in results if isinstance(r, HTTPException)] == [409]


async def test_batch_review_may_mix_resets_and_decisions():
    first, second = await machine("A"), await machine("B")
    await review(first.id, "reject", 1)

    rows = await annotations.review_annotation_batch(
        AnnotationBatchReview(
            items=[
                {"id": second.id, "version": 1, "decision": "accept"},
                {"id": first.id, "version": 2, "decision": "reset"},
            ]
        ),
        user("reviewer"),
    )

    assert [row.version for row in rows] == [2, 3]
    assert rows[0].body.provenance["review_status"] == "accepted"
    assert "review_status" not in rows[1].body.provenance
    assert set(await listed("reviewer")) == {first.id, second.id}


async def test_batch_reset_applies_the_reset_rules():
    mine, theirs, fresh = await machine("A"), await machine("B"), await machine("C")
    await review(mine.id, "accept", 1)
    await review(theirs.id, "accept", 1, reviewer="other")

    def batch(*items):
        return AnnotationBatchReview(
            items=[{"id": row.id, "version": version, "decision": "reset"} for row, version in items]
        )

    with pytest.raises(HTTPException) as forbidden:
        await annotations.review_annotation_batch(batch((mine, 2), (theirs, 2)), user("reviewer"))
    assert forbidden.value.status_code == 403 and forbidden.value.detail["index"] == 1

    with pytest.raises(HTTPException) as nothing:
        await annotations.review_annotation_batch(batch((mine, 2), (fresh, 1)), user("reviewer"))
    assert nothing.value.status_code == 422 and nothing.value.detail[0]["loc"] == ["body", "items", 1]

    with pytest.raises(HTTPException) as stale:
        await annotations.review_annotation_batch(batch((mine, 1)), user("reviewer"))
    assert stale.value.status_code == 409
    assert stale.value.detail["conflicts"][0]["current_version"] == 2

    assert (await listed("reviewer"))[mine.id].body.provenance["review_status"] == "accepted"


def test_a_review_decision_is_accept_reject_or_reset():
    for decision in ("accept", "reject", "reset"):
        assert AnnotationReview(decision=decision, version=1)
    with pytest.raises(ValidationError):
        AnnotationReview(decision="undo", version=1)


# --- soft delete -------------------------------------------------------------


async def test_a_deleted_annotation_leaves_every_read_and_write_path():
    row = await machine()
    await annotation_comments.create_comment(row.id, CommentIn(body="hi"), user("reader"))
    comment = (await annotation_comments.list_comments(row.id, user("reader"))).comments[0]
    await annotations.delete_annotation(row.id, user("loader"))

    assert row.id not in await listed("loader", include_rejected=True)
    assert row.id not in await listed("reader", include_rejected=True)
    assert await annotations._get_existing(row.id) is None
    assert await status_of(
        annotations.update_annotation(row.id, AnnotationUpdate(body=AnnotationBody(label="x"), version=1), user("loader"))
    ) == 404
    assert await status_of(review(row.id, "accept", 1)) == 404
    assert await status_of(annotations.delete_annotation(row.id, user("loader"))) == 404
    for call in (
        annotation_comments.list_comments(row.id, user("reader")),
        annotation_comments.create_comment(row.id, CommentIn(body="x"), user("reader")),
        annotation_comments.update_comment(row.id, comment.id, CommentIn(body="x"), user("reader")),
        annotation_comments.delete_comment(row.id, comment.id, user("reader")),
        # The creator, too, until it is restored.
        annotation_comments.list_comments(row.id, user("loader")),
    ):
        assert await status_of(call) == 404
    for kind, batch in (
        ("update", annotations.update_annotation_batch(AnnotationBatchUpdate(items=[{"id": row.id, "version": 1}]), user("loader"))),
        ("delete", annotations.delete_annotation_batch(AnnotationBatchDelete(items=[{"id": row.id, "version": 1}]), user("loader"))),
        ("review", annotations.review_annotation_batch(
            AnnotationBatchReview(items=[{"id": row.id, "version": 1, "decision": "accept"}]), user("reader"))),
    ):
        with pytest.raises(HTTPException) as error:
            await batch
        assert (kind, error.value.status_code, error.value.detail["index"]) == (kind, 404, 0)

    # The row and its whole thread are kept, unchanged.
    [stored] = await raw_rows("annotations", "id", row.id)
    assert stored["deleted_at"] is not None and stored["version"] == 1
    assert len(await raw_rows("annotation_comments", "annotation_id", row.id)) == 1


async def test_delete_is_still_creator_only_and_study_scoped():
    row = await create(visible_to=[])
    assert await status_of(annotations.delete_annotation(row.id, user("other"))) == 403
    assert await status_of(annotations.delete_annotation(row.id, user("owner", study_id="study-2"))) == 403
    assert row.id in await listed()


async def test_other_annotations_counts_are_unaffected():
    kept, gone = await create("kept", visible_to=[]), await create("gone", visible_to=[])
    for row in (kept, gone):
        await annotation_comments.create_comment(row.id, CommentIn(body="c"), user("reader"))
    await annotations.delete_annotation(gone.id, user("owner"))
    rows = await listed("reader")
    assert list(rows) == [kept.id] and rows[kept.id].comment_count == 1


async def test_slide_notes_are_unaffected_by_annotation_deletes():
    note = await slide_notes.create_slide_note(
        SlideNoteIn(slide_id=SLIDE_KEY, study_id="study-1", text="Margin", visible_to=[]), user("owner")
    )
    row = await create(visible_to=[])
    await annotations.delete_annotation(row.id, user("owner"))
    await annotations.purge_deleted_annotations(0)
    page = await slide_notes.list_slide_notes(SLIDE_KEY, user("reader"))
    assert [item.id for item in page.notes] == [note.id]


async def test_the_assistant_does_not_replay_a_deleted_annotation():
    kept, gone = await create(visible_to=[]), await create(visible_to=[])
    await annotations.delete_annotation(gone.id, user("owner"))
    async with annotations.storage_transaction() as tx:
        rows = await agent._committed_annotation_rows([kept.id, gone.id], tx._handle)
    assert [row["id"] for row in rows] == [kept.id]


# --- restore -----------------------------------------------------------------


async def restore(row_id: str, version: int, who: str = "owner", study_id: str = "study-1"):
    return await annotations.restore_annotation(
        row_id, AnnotationRestore(version=version), user(who, study_id=study_id)
    )


async def test_restore_brings_back_the_annotation_and_its_thread():
    row = await create(visible_to=[])
    first = await annotation_comments.create_comment(row.id, CommentIn(body="first"), user("reader"))
    removed = await annotation_comments.create_comment(row.id, CommentIn(body="removed"), user("reader"))
    await annotation_comments.delete_comment(row.id, removed.id, user("reader"))
    await annotations.delete_annotation(row.id, user("owner"))

    restored = await restore(row.id, 1)

    assert restored.id == row.id
    assert restored.version == 2
    assert restored.comment_count == 1
    assert restored.body == row.body and restored.target == row.target
    assert restored.visible_to == [] and restored.created_by == "owner"
    assert restored.created_at == row.created_at
    assert (await listed("reader"))[row.id].comment_count == 1
    page = await annotation_comments.list_comments(row.id, user("reader"))
    assert [item.id for item in page.comments] == [first.id]
    # The thread is writable again, and a stale writer's version is refused.
    await annotation_comments.create_comment(row.id, CommentIn(body="back"), user("reader"))
    assert await status_of(
        annotations.update_annotation(row.id, AnnotationUpdate(body=AnnotationBody(label="x"), version=1), user("owner"))
    ) == 409
    updated = await annotations.update_annotation(
        row.id, AnnotationUpdate(body=AnnotationBody(label="x"), version=2), user("owner")
    )
    assert updated.version == 3


async def test_restore_rules():
    row = await create(visible_to=[])
    live = await create(visible_to=[])
    await annotations.delete_annotation(row.id, user("owner"))

    assert await status_of(restore(row.id, 1, who="other")) == 403
    assert await status_of(restore(row.id, 1, study_id="study-2")) == 403
    assert await status_of(restore(row.id, 2)) == 409
    assert await status_of(restore(live.id, 1)) == 404
    assert await status_of(restore("missing", 1)) == 404
    assert row.id not in await listed()

    await restore(row.id, 1)
    assert await status_of(restore(row.id, 2)) == 404


async def test_a_rejected_annotation_stays_rejected_after_restore():
    row = await machine()
    await review(row.id, "reject", 1)
    await annotations.delete_annotation(row.id, user("loader"))
    restored = await restore(row.id, 2, who="loader")
    assert restored.body.provenance["review_status"] == "rejected"
    assert row.id not in await listed("loader")
    assert row.id in await listed("loader", include_rejected=True)


async def test_concurrent_restores_take_one():
    row = await create()
    await annotations.delete_annotation(row.id, user("owner"))
    results = await asyncio.gather(restore(row.id, 1), restore(row.id, 1), return_exceptions=True)
    assert sum(isinstance(r, annotations.AnnotationOut) for r in results) == 1
    assert [r.status_code for r in results if isinstance(r, HTTPException)] in ([404], [409])
    assert (await listed())[row.id].version == 2


async def test_batch_delete_and_batch_restore():
    rows = [await create(str(i), visible_to=[]) for i in range(3)]
    await annotation_comments.create_comment(rows[0].id, CommentIn(body="c"), user("reader"))
    deleted = await annotations.delete_annotation_batch(
        AnnotationBatchDelete(items=[{"id": rows[0].id, "version": 1}, {"id": rows[1].id, "version": 1}]),
        user("owner"),
    )
    assert [row.version for row in deleted] == [1, 1]
    assert list(await listed()) == [rows[2].id]

    restored = await annotations.restore_annotation_batch(
        AnnotationBatchRestore(items=[{"id": rows[1].id, "version": 1}, {"id": rows[0].id, "version": 1}]),
        user("owner"),
    )

    assert [(row.id, row.version) for row in restored] == [(rows[1].id, 2), (rows[0].id, 2)]
    assert restored[1].comment_count == 1
    assert set(await listed()) == {row.id for row in rows}


async def test_batch_restore_rules():
    mine = await create(visible_to=[])
    theirs = await create(owner="other", visible_to=[])
    live = await create(visible_to=[])
    for row, owner in ((mine, "owner"), (theirs, "other")):
        await annotations.delete_annotation(row.id, user(owner))

    def batch(*items):
        return AnnotationBatchRestore(items=[{"id": row.id, "version": version} for row, version in items])

    with pytest.raises(HTTPException) as forbidden:
        await annotations.restore_annotation_batch(batch((mine, 1), (theirs, 1)), user("owner"))
    assert forbidden.value.status_code == 403 and forbidden.value.detail["index"] == 1

    with pytest.raises(HTTPException) as not_deleted:
        await annotations.restore_annotation_batch(batch((mine, 1), (live, 1)), user("owner"))
    assert not_deleted.value.status_code == 404 and not_deleted.value.detail["index"] == 1

    with pytest.raises(HTTPException) as stale:
        await annotations.restore_annotation_batch(batch((mine, 4)), user("owner"))
    assert stale.value.status_code == 409
    assert stale.value.detail["conflicts"][0]["current_version"] == 1

    with pytest.raises(HTTPException) as other_study:
        await annotations.restore_annotation_batch(batch((mine, 1)), user("owner", study_id="study-2"))
    assert other_study.value.status_code == 403

    with pytest.raises(HTTPException) as repeated:
        await annotations.restore_annotation_batch(batch((mine, 1), (mine, 1)), user("owner"))
    assert repeated.value.status_code == 422

    # Nothing was restored by the failed batches.
    assert set(await listed()) == {live.id}


# --- retention purge and orphan sweep ---------------------------------------


async def test_the_purge_removes_only_expired_deletions_with_their_threads():
    expired, recent, live = [await create(str(i), visible_to=[]) for i in range(3)]
    for row in (expired, recent, live):
        await annotation_comments.create_comment(row.id, CommentIn(body="c"), user("reader"))
    for row in (expired, recent):
        await annotations.delete_annotation(row.id, user("owner"))
    await age_deletion(expired.id, 31)
    await age_deletion(recent.id, 29)

    assert await annotations.purge_deleted_annotations(30) == 1
    assert await annotations.purge_deleted_annotations(30) == 0

    assert await raw_rows("annotations", "id", expired.id) == []
    assert await raw_rows("annotation_comments", "annotation_id", expired.id) == []
    assert len(await raw_rows("annotation_comments", "annotation_id", recent.id)) == 1
    assert (await restore(recent.id, 1)).comment_count == 1
    assert await status_of(restore(expired.id, 1)) == 404
    assert set(await listed()) == {recent.id, live.id}


async def test_the_purge_never_touches_live_annotations():
    row = await create(visible_to=[])
    async with annotations.storage_transaction(write=True) as tx:
        old = "timezone('utc', now()) - interval '400 days'" if tx.kind == "postgres" else "datetime('now', '-400 days')"
        await tx.execute(f"UPDATE annotations SET created_at = {old}, updated_at = {old} WHERE id = $1", row.id)
    assert await annotations.purge_deleted_annotations(1) == 0
    assert row.id in await listed()


async def test_the_purge_follows_the_retention_setting(monkeypatch):
    row = await create()
    await annotations.delete_annotation(row.id, user("owner"))
    await age_deletion(row.id, 400)

    monkeypatch.setattr(annotations.settings, "annotation_deleted_retention_days", 0)
    assert await annotations.purge_deleted_annotations() == 0
    assert await annotations.purge_deleted_annotations(0) == 0
    monkeypatch.setattr(annotations.settings, "annotation_deleted_retention_days", 500)
    assert await annotations.purge_deleted_annotations() == 0
    monkeypatch.setattr(annotations.settings, "annotation_deleted_retention_days", 30)
    assert await annotations.purge_deleted_annotations() == 1


async def test_the_orphan_sweep_keeps_a_deleted_annotations_thread():
    row = await create(visible_to=[])
    await annotation_comments.create_comment(row.id, CommentIn(body="c"), user("reader"))
    await annotations.delete_annotation(row.id, user("owner"))
    assert await annotations.sweep_orphan_comments() == 0
    assert len(await raw_rows("annotation_comments", "annotation_id", row.id)) == 1


async def test_a_restart_purges_expired_deletions(monkeypatch):
    row = await create()
    await annotations.delete_annotation(row.id, user("owner"))
    await age_deletion(row.id, 31)
    monkeypatch.setattr(annotations.settings, "annotation_deleted_retention_days", 30)
    await annotations.init_db(annotations._get_db_path(), db_url=annotations._get_db_url())
    assert await raw_rows("annotations", "id", row.id) == []


async def test_the_daily_purge_loop_runs_until_cancelled(monkeypatch):
    calls = []

    async def purge():
        calls.append(1)
        return 0

    monkeypatch.setattr(annotations, "purge_deleted_annotations", purge)
    task = asyncio.create_task(annotations.purge_deleted_annotations_forever(0.001))
    while len(calls) < 2:
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
