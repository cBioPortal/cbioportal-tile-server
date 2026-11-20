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
    # Slide keys are 12 lowercase hex characters.
    slide_id = suffix[:12]
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
                selector={"type": "SvgSelector", "value": "<svg><polygon points=\"1,1 4,1 4,4\" /></svg>"}
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


@pytest.mark.asyncio
async def test_concurrent_migrations_on_empty_schema_all_succeed():
    """Workers starting together against an empty database all migrate cleanly."""
    import asyncio

    import asyncpg

    from app.annotation_db import migrate, migrate_agent

    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.fail("POSTGRES_TEST_DSN is required for the PostgreSQL integration suite")

    schema = f"migrate_race_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    workers = 8
    try:
        connections = await asyncio.gather(
            *(
                asyncpg.connect(dsn, server_settings={"search_path": schema})
                for _ in range(workers)
            )
        )
        try:
            start = asyncio.Event()

            async def start_worker(conn: asyncpg.Connection) -> None:
                await start.wait()
                await migrate(conn)
                await migrate_agent(conn)

            tasks = [asyncio.create_task(start_worker(conn)) for conn in connections]
            await asyncio.sleep(0)
            start.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await asyncio.gather(*(conn.close() for conn in connections))

        assert [r for r in results if isinstance(r, BaseException)] == []
        tables = {
            row["table_name"]
            for row in await admin.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = $1",
                schema,
            )
        }
        assert {
            "annotation_schema_migrations",
            "annotations",
            "annotation_comments",
            "slide_notes",
            "agent_actions",
            "agent_retrieval_candidates",
            "agent_retrieval_runs",
        } <= tables
        versions = await admin.fetch(
            f'SELECT component, version FROM "{schema}".annotation_schema_migrations'
        )
        assert {row["component"]: row["version"] for row in versions} == {
            "annotations": 3,
            "agent_actions": 1,
            "agent_retrieval_candidates": 1,
            "agent_retrieval_runs": 1,
        }
    finally:
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_postgres_batch_is_atomic_and_review_bumps_the_version(monkeypatch):
    dsn = os.environ.get("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.fail("POSTGRES_TEST_DSN is required for the PostgreSQL integration suite")

    from app import annotation_db

    # The pool belongs to the event loop that opened it; this test has its own.
    monkeypatch.setattr(annotation_db, "_pool", None)
    monkeypatch.setattr(annotation_db, "_pool_dsn", "")
    await annotation_store.init_db(db_url=dsn)
    suffix = uuid.uuid4().hex
    slide_id = suffix[:12]
    loader = {"sub": f"loader-{suffix}", "groups": [], "study_id": "integration-study"}
    reviewer = {"sub": f"reviewer-{suffix}", "groups": [], "study_id": "integration-study"}
    monkeypatch.setattr(annotation_store.settings, "annotation_trusted_writers", [loader["sub"]])

    def item(label: str) -> annotation_store.AnnotationIn:
        return annotation_store.AnnotationIn(
            slide_id=slide_id,
            study_id="integration-study",
            body=annotation_store.AnnotationBody(
                label=label, provenance={"source": "precomputed"}
            ),
            target=annotation_store.AnnotationTarget(
                selector={"type": "SvgSelector", "value": "<svg><polygon points=\"1,1 4,1 4,4\" /></svg>"}
            ),
            visible_to=[],
        )

    real_insert = annotation_store._insert_postgres_connection
    calls = 0

    async def failing_second_insert(conn, data, user_sub, annotation_id=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("storage failed")
        return await real_insert(conn, data, user_sub, annotation_id)

    monkeypatch.setattr(annotation_store, "_insert_postgres_connection", failing_second_insert)
    with pytest.raises(RuntimeError):
        await annotation_store.create_annotation_batch(
            annotation_store.AnnotationBatchIn(annotations=[item("a"), item("b")]), loader
        )
    monkeypatch.setattr(annotation_store, "_insert_postgres_connection", real_insert)
    assert await annotation_store.list_annotations(slide_id, loader) == []

    created = await annotation_store.create_annotation_batch(
        annotation_store.AnnotationBatchIn(annotations=[item("a"), item("b")]), loader
    )
    assert [row.body.label for row in created] == ["a", "b"]

    rejected = await annotation_store.review_annotation(
        created[0].id,
        annotation_store.AnnotationReview(decision="reject", version=1),
        reviewer,
    )
    assert rejected.version == 2
    assert rejected.updated_at.endswith("Z")
    assert rejected.body.provenance["reviewed_by"] == reviewer["sub"]
    listed = await annotation_store.list_annotations(slide_id, reviewer)
    assert [row.id for row in listed] == [created[1].id]

    for row in created:
        await annotation_store._delete_postgres(row.id)
    await annotation_store.close_db()
