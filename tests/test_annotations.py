import asyncio

import pytest
from fastapi import HTTPException

from app import annotations
from app.annotations import (
    AnnotationBody,
    AnnotationIn,
    AnnotationOut,
    AnnotationTarget,
    AnnotationUpdate,
)


def user(
    subject: str,
    *,
    groups: list[str] | None = None,
    study_id: str | None = "study-1",
) -> dict:
    return {
        "sub": subject,
        "groups": groups or [],
        "study_id": study_id,
    }


def annotation_input(
    *,
    slide_id: str = "slide-1",
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

    listed = await annotations.list_annotations("slide-1", "study-1", owner)
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
    assert await annotations.list_annotations("slide-1", "study-1", owner) == []


@pytest.mark.asyncio
async def test_annotation_visibility_honors_creator_groups_and_public_rows():
    owner = user("owner")
    private = await annotations.create_annotation(annotation_input(), owner)
    group = await annotations.create_annotation(
        annotation_input(visible_to=["pathology"]), owner
    )
    public = await annotations.create_annotation(annotation_input(visible_to=[]), owner)

    outsider_rows = await annotations.list_annotations(
        "slide-1", "study-1", user("outsider")
    )
    assert [item.id for item in outsider_rows] == [public.id]

    group_rows = await annotations.list_annotations(
        "slide-1", "study-1", user("reviewer", groups=["pathology"])
    )
    assert {item.id for item in group_rows} == {group.id, public.id}

    owner_rows = await annotations.list_annotations("slide-1", "study-1", owner)
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

    with pytest.raises(HTTPException) as wrong_study_list:
        await annotations.list_annotations(
            "slide-1", "study-1", user("owner", study_id="study-2")
        )
    assert wrong_study_list.value.status_code == 403

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
