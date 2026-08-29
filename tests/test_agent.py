import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import agent


def make_context() -> agent.AgentContext:
    return agent.AgentContext(
        study_id="study-a",
        patient_id="patient-a",
        sample_id="sample-a",
        slide_id="slide-a",
        filters={"stain_filter": "hne"},
        slide_metadata={"magnification": "20x"},
        patient_context={"sample": {"sample_id": "sample-a"}},
        viewport=agent.ViewportContext(
            slide_width=1000,
            slide_height=800,
            image_data_url="data:image/jpeg;base64,"
            + base64.b64encode(b"jpeg").decode(),
            image_width=100,
            image_height=80,
            image_transform=[1, 0, 0, 0, 1, 0],
        ),
    )


@pytest.fixture
async def agent_db(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    monkeypatch.setattr(agent.settings, "annotation_database_url", "")
    await agent.init_db(db_path=str(db_path), db_url="")
    agent._rate_windows.clear()
    yield


@pytest.mark.asyncio
async def test_pending_proposal_requires_single_approval(agent_db):
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-a", context=make_context()
    )
    proposal = await agent._insert_action(
        run_context,
        "create_annotation",
        {"geometry_type": "rectangle", "points": [{"x": 1, "y": 2}]},
    )
    assert proposal.status == "pending"

    approved = await agent._change_action_status(
        proposal.id, "user-a", "pending", "approved"
    )
    assert approved is not None
    assert approved.status == "approved"
    assert (
        await agent._change_action_status(proposal.id, "user-a", "pending", "approved")
        is None
    )


@pytest.mark.asyncio
async def test_rejected_proposal_cannot_be_applied(agent_db):
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-a", context=make_context()
    )
    proposal = await agent._insert_action(
        run_context, "viewer_action", {"action": "zoom"}
    )
    rejected = await agent._change_action_status(
        proposal.id, "user-a", "pending", "rejected"
    )
    assert rejected is not None and rejected.status == "rejected"
    assert (
        await agent._change_action_status(proposal.id, "user-a", "pending", "approved")
        is None
    )


@pytest.mark.asyncio
async def test_context_validation_rejects_non_jpeg_and_large_patient_context():
    context = make_context()
    context.viewport.image_data_url = (
        "data:image/png;base64," + base64.b64encode(b"x").decode()
    )
    with pytest.raises(HTTPException) as error:
        agent._validate_context(context)
    assert error.value.status_code == 422

    context = make_context()
    context.patient_context = {"payload": "x" * (65 * 1024)}
    with pytest.raises(HTTPException) as error:
        agent._validate_context(context)
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_rate_limit_is_scoped_to_user(agent_db, monkeypatch):
    monkeypatch.setattr(agent.settings, "agent_rate_limit_per_minute", 2)
    await agent._check_rate_limit("user-a")
    await agent._check_rate_limit("user-a")
    await agent._check_rate_limit("user-b")
    with pytest.raises(HTTPException) as error:
        await agent._check_rate_limit("user-a")
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "60"


def test_api_key_file_is_read_without_exposing_contents(tmp_path, monkeypatch):
    key = "sk-test-secret-value"
    key_file = tmp_path / "openai_key.txt"
    key_file.write_text(key + "\n", encoding="utf-8")
    monkeypatch.setattr(agent.settings, "agent_api_key_file", str(key_file))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert agent._ensure_openai_credentials() is True
    assert agent.os.environ["OPENAI_API_KEY"] == key


@pytest.mark.asyncio
async def test_action_list_is_session_and_user_scoped(agent_db):
    context = make_context()
    first = agent.AgentRunContext("user-a", "session-a", context)
    second = agent.AgentRunContext(
        "user-b", "session-a", context.model_copy(update={"study_id": "study-a"})
    )
    await agent._insert_action(first, "viewer_action", {"action": "zoom"})
    await agent._insert_action(second, "viewer_action", {"action": "zoom"})
    actions = await agent._list_actions("session-a", "user-a", "study-a")
    assert len(actions) == 1
    assert json.loads(json.dumps(actions[0].model_dump()))["status"] == "pending"


@pytest.mark.asyncio
async def test_stream_emits_text_and_completion_without_writing(agent_db, monkeypatch):
    class FakeStream:
        async def stream_events(self):
            yield SimpleNamespace(
                type="raw_response_event",
                data=SimpleNamespace(
                    type="response.output_text.delta", delta="viewport summary"
                ),
            )

    monkeypatch.setattr(
        agent.Runner, "run_streamed", lambda *args, **kwargs: FakeStream()
    )
    request = agent.ChatRequest(
        session_id="session-a", message="Summarize this", context=make_context()
    )
    events = [chunk async for chunk in agent._stream_agent(request, "user-a")]
    assert any(
        "event: message.delta" in chunk and "viewport summary" in chunk
        for chunk in events
    )
    assert events[-1].startswith("event: complete")
    assert await agent._list_actions("session-a", "user-a", "study-a") == []
