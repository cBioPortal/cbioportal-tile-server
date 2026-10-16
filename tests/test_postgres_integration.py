import os
import uuid

import pytest

from app import agent
from app import annotations as annotation_store


pytestmark = pytest.mark.integration


def _context(slide_id: str, patient_id: str) -> agent.AgentContext:
    return agent.AgentContext(
        study_id="integration-study",
        patient_id=patient_id,
        slide_id=slide_id,
        filters={},
        slide_metadata={},
        patient_context={},
        viewport=agent.ViewportContext(
            slide_width=1000,
            slide_height=800,
            source_fingerprint="integration-source",
            capture_id="integration-capture",
            viewer_generation=1,
        ),
    )


@pytest.mark.asyncio
async def test_postgres_round_trip_uses_real_timestamp_columns():
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.fail("POSTGRES_TEST_DSN is required for the PostgreSQL integration suite")

    await annotation_store.init_db(db_url=dsn)
    await agent.init_db(db_url=dsn)

    suffix = uuid.uuid4().hex
    slide_id = f"integration-slide-{suffix}"
    user_sub = f"integration-user-{suffix}"
    context = _context(slide_id, f"integration-patient-{suffix}")
    run_context = agent.AgentRunContext(
        user_sub=user_sub,
        session_id=f"integration-session-{suffix}",
        context=context,
    )

    action = await agent._insert_action(
        run_context,
        "viewer_action",
        {"action": "zoom", "parameters": {"zoom": 2}},
    )
    assert action.created_at.endswith("+00:00") or action.created_at.endswith("Z")
    approved = await agent._change_action_status(
        action.id, user_sub, "pending", "approved"
    )
    assert approved is not None and approved.status == "approved"
    assert approved.decided_at is not None

    annotation = await annotation_store._create_postgres(
        annotation_store.AnnotationIn(
            slide_id=slide_id,
            study_id=context.study_id,
            body=annotation_store.AnnotationBody(
                label="integration",
                comment="Integration",
                type="#3b82f6",
            ),
            target=annotation_store.AnnotationTarget(
                selector={"type": "SvgSelector", "value": "<svg />"}
            ),
        ),
        user_sub,
    )
    assert annotation["created_at"].endswith("Z")
    listed = await annotation_store._list_postgres(
        slide_id, context.study_id, user_sub
    )
    assert len(listed) == 1
    assert listed[0]["version"] == 1

    updated_at = await annotation_store._update_postgres(
        annotation["id"],
        annotation["body"],
        annotation["target"],
        annotation["visible_to"],
        expected_version=1,
        new_version=2,
    )
    assert updated_at is not None and updated_at.endswith("Z")

    await annotation_store._delete_postgres(annotation["id"])
    assert await annotation_store._get_existing_postgres(annotation["id"]) is None
