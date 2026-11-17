import asyncio

import pytest
from fastapi import HTTPException

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import annotations
from app.annotations import (
    AnnotationBody,
    AnnotationBatchIn,
    AnnotationIn,
    AnnotationOut,
    AnnotationTarget,
    AnnotationUpdate,
)

SLIDE_KEY = "0123456789ab"


def user(
    subject: str,
    *,
    groups: list[str] | None = None,
    study_id: str = "study-1",
) -> dict:
    return {
        "sub": subject,
        "groups": groups or [],
        "study_id": study_id,
    }


def annotation_input(
    *,
    slide_id: str = SLIDE_KEY,
    study_id: str = "study-1",
    visible_to: list[str] | None = None,
) -> AnnotationIn:
    return AnnotationIn(
        slide_id=slide_id,
        study_id=study_id,
        body=AnnotationBody(label="Tumor", comment="Initial", type="Polygon"),
        target=AnnotationTarget(
            selector={
                "type": "SvgSelector",
                "value": "<svg><polygon points='1,1 4,1 4,4'/></svg>",
            }
        ),
        visible_to=visible_to,
    )


@pytest.fixture(autouse=True)
async def sqlite_annotation_db(tmp_path, monkeypatch):
    monkeypatch.setattr(annotations.settings, "annotation_database_url", "")
    await annotations.init_db(str(tmp_path / "annotations.db"), db_url="")
    yield
    annotations._db_path = ""
    annotations._db_url = ""


@pytest.mark.asyncio
async def test_annotation_crud_round_trip_and_privacy_reset():
    owner = user("owner")
    created = await annotations.create_annotation(
        annotation_input(visible_to=["pathology"]), owner
    )

    assert created.version == 1
    assert created.created_by == "owner"
    assert created.visible_to == ["pathology"]
    assert created.created_at
    assert created.updated_at

    listed = await annotations.list_annotations(SLIDE_KEY, owner)
    assert [item.id for item in listed] == [created.id]

    updated = await annotations.update_annotation(
        created.id,
        AnnotationUpdate(
            body=AnnotationBody(label="Tumor", comment="Reviewed", type="Polygon"),
            visible_to=None,
            version=1,
        ),
        owner,
    )
    assert updated.version == 2
    assert updated.body.comment == "Reviewed"
    assert updated.visible_to is None
    assert updated.updated_at

    await annotations.delete_annotation(created.id, owner)
    assert await annotations.list_annotations(SLIDE_KEY, owner) == []


@pytest.mark.asyncio
async def test_annotation_batch_preserves_layer_color_and_provenance():
    owner = user("owner")
    batch = AnnotationBatchIn(
        annotations=[
            annotation_input(),
            AnnotationIn(
                slide_id=SLIDE_KEY,
                study_id="study-1",
                body=AnnotationBody(
                    label="AI region",
                    type="Polygon",
                    layer_name="AI review",
                    color="#7b61ff",
                    provenance={"source": "agent", "model": "quiltnet_pmb"},
                ),
                target=AnnotationTarget(selector={"type": "SvgSelector", "value": "<svg/>"}),
            ),
        ]
    )
    created = await annotations.create_annotation_batch(batch, owner)

    assert len(created) == 2
    assert created[1].body.layer_name == "AI review"
    assert created[1].body.color == "#7b61ff"
    assert created[1].body.provenance == {"source": "agent", "model": "quiltnet_pmb"}


@pytest.mark.asyncio
async def test_annotation_visibility_honors_creator_groups_and_public_rows():
    owner = user("owner")
    private = await annotations.create_annotation(annotation_input(), owner)
    group = await annotations.create_annotation(
        annotation_input(visible_to=["pathology"]), owner
    )
    public = await annotations.create_annotation(annotation_input(visible_to=[]), owner)

    outsider_rows = await annotations.list_annotations(SLIDE_KEY, user("outsider"))
    assert [item.id for item in outsider_rows] == [public.id]

    group_rows = await annotations.list_annotations(
        SLIDE_KEY, user("reviewer", groups=["pathology"])
    )
    assert {item.id for item in group_rows} == {group.id, public.id}

    owner_rows = await annotations.list_annotations(SLIDE_KEY, owner)
    assert {item.id for item in owner_rows} == {private.id, group.id, public.id}


@pytest.mark.asyncio
async def test_annotation_writes_require_creator_and_matching_study_scope():
    owner = user("owner")
    created = await annotations.create_annotation(annotation_input(), owner)

    with pytest.raises(HTTPException) as wrong_study_create:
        await annotations.create_annotation(
            annotation_input(), user("owner", study_id="study-2")
        )
    assert wrong_study_create.value.status_code == 403

    with pytest.raises(HTTPException) as wrong_study_update:
        await annotations.update_annotation(
            created.id, AnnotationUpdate(version=1), user("owner", study_id="study-2")
        )
    assert wrong_study_update.value.status_code == 403

    with pytest.raises(HTTPException) as wrong_study_delete:
        await annotations.delete_annotation(created.id, user("owner", study_id="study-2"))
    assert wrong_study_delete.value.status_code == 403

    with pytest.raises(HTTPException) as non_creator_update:
        await annotations.update_annotation(
            created.id,
            AnnotationUpdate(version=1),
            user("other"),
        )
    assert non_creator_update.value.status_code == 403

    with pytest.raises(HTTPException) as non_creator_delete:
        await annotations.delete_annotation(created.id, user("other"))
    assert non_creator_delete.value.status_code == 403


@pytest.mark.asyncio
async def test_local_development_subject_can_manage_legacy_annotations(monkeypatch):
    monkeypatch.setattr(annotations.settings, "annotation_local_development", True)
    created = await annotations.create_annotation(annotation_input(), user("legacy-owner"))

    await annotations.delete_annotation(created.id, user("local-development"))

    assert await annotations.list_annotations(SLIDE_KEY, user("local-development")) == []


@pytest.mark.asyncio
async def test_annotation_update_uses_atomic_optimistic_locking():
    owner = user("owner")
    created = await annotations.create_annotation(annotation_input(), owner)

    updates = await asyncio.gather(
        annotations.update_annotation(
            created.id,
            AnnotationUpdate(
                body=AnnotationBody(label="A", comment="First", type="Polygon"),
                version=1,
            ),
            owner,
        ),
        annotations.update_annotation(
            created.id,
            AnnotationUpdate(
                body=AnnotationBody(label="B", comment="Second", type="Polygon"),
                version=1,
            ),
            owner,
        ),
        return_exceptions=True,
    )

    successes = [result for result in updates if isinstance(result, AnnotationOut)]
    conflicts = [result for result in updates if isinstance(result, HTTPException)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].status_code == 409


@pytest.mark.parametrize("slide_id", ["470775", "slide-1", "S12-34567", "0123456789ABCDEF0123456789ABCDEF"])
def test_annotations_accept_only_opaque_slide_keys(slide_id):
    with pytest.raises(ValidationError):
        annotation_input(slide_id=slide_id)

    app = FastAPI()
    app.include_router(annotations.router)
    app.dependency_overrides[annotations.require_user] = lambda: user("owner")
    response = TestClient(app).get(
        "/annotations", params={"slide_id": slide_id, "studyId": "study-1"}
    )
    assert response.status_code == 422



def machine_input(source: str = "precomputed", visible_to: list[str] | None = None) -> AnnotationIn:
    data = annotation_input(visible_to=[] if visible_to is None else visible_to)
    data.body = AnnotationBody(
        label="Tumor",
        type="Polygon",
        layer_name="Model",
        provenance={"source": source, "model": "luad-patterns"},
    )
    return data


@pytest.mark.parametrize("source", ["agent", "assistant", "AI", "model", "precomputed"])
def test_machine_sources_are_reviewable(source):
    assert annotations.is_machine_made({"provenance": {"source": source}})


@pytest.mark.parametrize(
    "body",
    [{}, {"provenance": None}, {"provenance": {"source": "import"}}, {"provenance": {"source": 1}}, "not json"],
)
def test_other_annotations_are_not_machine_made(body):
    assert not annotations.is_machine_made(body)


@pytest.mark.asyncio
async def test_any_user_in_the_study_may_accept_a_machine_annotation():
    created = await annotations.create_annotation(machine_input(), user("loader"))

    accepted = await annotations.review_annotation(
        created.id, annotations.AnnotationReview(decision="accept", version=1), user("reviewer")
    )

    assert accepted.version == 2
    assert accepted.created_by == "loader"
    assert accepted.body.label == "Tumor"
    assert accepted.target == created.target
    assert accepted.visible_to == []
    provenance = accepted.body.provenance
    assert provenance["source"] == "precomputed"
    assert provenance["model"] == "luad-patterns"
    assert provenance["review_status"] == "accepted"
    assert provenance["reviewed_by"] == "reviewer"
    assert provenance["reviewed_at"].endswith("Z")
    listed = await annotations.list_annotations(SLIDE_KEY, user("other"))
    assert [item.body.provenance["review_status"] for item in listed] == ["accepted"]


@pytest.mark.asyncio
async def test_a_rejected_annotation_is_kept_but_not_listed():
    created = await annotations.create_annotation(machine_input("agent"), user("loader"))

    rejected = await annotations.review_annotation(
        created.id, annotations.AnnotationReview(decision="reject", version=1), user("reviewer")
    )

    assert rejected.body.provenance["review_status"] == "rejected"
    assert await annotations.list_annotations(SLIDE_KEY, user("loader")) == []
    audit = await annotations.list_annotations(SLIDE_KEY, user("loader"), include_rejected=True)
    assert [item.id for item in audit] == [created.id]

    # A later review may restore it, and its creator may still delete it.
    restored = await annotations.review_annotation(
        created.id, annotations.AnnotationReview(decision="accept", version=2), user("second")
    )
    assert restored.version == 3
    assert [item.id for item in await annotations.list_annotations(SLIDE_KEY, user("x"))] == [created.id]
    await annotations.delete_annotation(created.id, user("loader"))


@pytest.mark.asyncio
async def test_review_refuses_human_annotations_other_studies_and_stale_versions():
    human = await annotations.create_annotation(annotation_input(visible_to=[]), user("owner"))
    with pytest.raises(HTTPException) as not_machine:
        await annotations.review_annotation(
            human.id, annotations.AnnotationReview(decision="accept", version=1), user("owner")
        )
    assert not_machine.value.status_code == 403

    machine = await annotations.create_annotation(machine_input(), user("loader"))
    with pytest.raises(HTTPException) as wrong_study:
        await annotations.review_annotation(
            machine.id,
            annotations.AnnotationReview(decision="accept", version=1),
            user("reviewer", study_id="study-2"),
        )
    assert wrong_study.value.status_code == 403

    with pytest.raises(HTTPException) as stale:
        await annotations.review_annotation(
            machine.id, annotations.AnnotationReview(decision="reject", version=5), user("reviewer")
        )
    assert stale.value.status_code == 409

    with pytest.raises(HTTPException) as missing:
        await annotations.review_annotation(
            "missing", annotations.AnnotationReview(decision="accept", version=1), user("reviewer")
        )
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_review_needs_the_annotation_to_be_visible_to_the_reviewer():
    private = await annotations.create_annotation(
        machine_input(visible_to=None).model_copy(update={"visible_to": None}), user("loader")
    )
    with pytest.raises(HTTPException) as hidden:
        await annotations.review_annotation(
            private.id, annotations.AnnotationReview(decision="accept", version=1), user("reviewer")
        )
    assert hidden.value.status_code == 404


@pytest.mark.asyncio
async def test_review_does_not_open_edit_or_delete_to_other_users():
    machine = await annotations.create_annotation(machine_input(), user("loader"))
    with pytest.raises(HTTPException) as update:
        await annotations.update_annotation(machine.id, AnnotationUpdate(version=1), user("reviewer"))
    assert update.value.status_code == 403
    with pytest.raises(HTTPException) as delete:
        await annotations.delete_annotation(machine.id, user("reviewer"))
    assert delete.value.status_code == 403


@pytest.mark.asyncio
async def test_concurrent_reviews_take_one_version():
    machine = await annotations.create_annotation(machine_input(), user("loader"))
    results = await asyncio.gather(
        annotations.review_annotation(
            machine.id, annotations.AnnotationReview(decision="accept", version=1), user("a")
        ),
        annotations.review_annotation(
            machine.id, annotations.AnnotationReview(decision="reject", version=1), user("b")
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, AnnotationOut) for result in results) == 1
    conflicts = [result for result in results if isinstance(result, HTTPException)]
    assert [conflict.status_code for conflict in conflicts] == [409]


def test_a_review_decision_must_be_accept_or_reject():
    with pytest.raises(ValidationError):
        annotations.AnnotationReview(decision="delete", version=1)


@pytest.mark.asyncio
async def test_annotation_batch_saves_all_or_none(monkeypatch):
    owner = user("owner")
    real_insert = annotations._insert_sqlite_connection
    calls = 0

    async def failing_second_insert(db, data, user_sub, annotation_id=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("storage failed")
        return await real_insert(db, data, user_sub, annotation_id)

    monkeypatch.setattr(annotations, "_insert_sqlite_connection", failing_second_insert)
    with pytest.raises(RuntimeError):
        await annotations.create_annotation_batch(
            AnnotationBatchIn(annotations=[annotation_input(visible_to=[]) for _ in range(3)]), owner
        )
    monkeypatch.setattr(annotations, "_insert_sqlite_connection", real_insert)

    assert await annotations.list_annotations(SLIDE_KEY, owner) == []

    created = await annotations.create_annotation_batch(
        AnnotationBatchIn(annotations=[annotation_input(visible_to=[]) for _ in range(3)]), owner
    )
    listed = await annotations.list_annotations(SLIDE_KEY, owner)
    assert {item.id for item in listed} == {item.id for item in created}
