import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import agent
from app import annotations as annotation_store


def make_context() -> agent.AgentContext:
    return agent.AgentContext(
        study_id="study-a",
        patient_id="patient-a",
        sample_id="sample-a",
        slide_id="slide-a",
        filters={"stain_filter": "hne"},
        slide_metadata={"magnification": "20x"},
        patient_context={"sample": {"sample_id": "sample-a"}},
        embedding_context=agent.EmbeddingContext(
            provider="quiltnet", scope="study", slide_ids=["slide-a", "slide-b"]
        ),
        viewport=agent.ViewportContext(
            slide_width=1000,
            slide_height=800,
            image_data_url="data:image/jpeg;base64,"
            + base64.b64encode(b"jpeg").decode(),
            image_width=100,
            image_height=80,
            image_transform=[1, 0, 0, 0, 1, 0],
            source_fingerprint="source-v2-fingerprint",
            capture_id="capture-1",
            viewer_generation=3,
        ),
    )


def test_agent_input_preserves_embedding_context():
    request = agent.ChatRequest(
        session_id="session-a", message="Summarize this", context=make_context()
    )

    prompt = json.loads(agent._agent_input(request)[0]["content"][0]["text"])

    assert prompt["current_context"]["embedding_context"] == {
        "provider": "quiltnet",
        "scope": "study",
        "slide_ids": ["slide-a", "slide-b"],
    }


def test_bedrock_region_tool_only_advertises_live_tile_model():
    tool = next(
        item["toolSpec"]
        for item in agent._bedrock_tools()
        if item["toolSpec"]["name"] == "wsi_find_regions"
    )
    schema = tool["inputSchema"]["json"]

    assert schema["properties"]["model"]["enum"] == ["quiltnet_pmb"]
    assert "tile-retrieval" in tool["description"]


@pytest.mark.asyncio
async def test_bedrock_similar_slides_does_not_relabel_another_model(monkeypatch):
    async def fake_manifest():
        return {
            "slides": [
                {
                    "study_id": "study-a",
                    "slide_id": "slide-a",
                    "similar_slides": [
                        {"slide_id": "titan-slide", "model": "reef_v2_titan"},
                    ],
                }
            ]
        }

    monkeypatch.setattr(agent, "_load_manifest", fake_manifest)
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-a", context=make_context()
    )

    result = await agent._bedrock_tool(
        "wsi_find_similar_slides",
        {"model": "reef_v2_optimus", "top_k": 5},
        run_context,
    )

    assert result == {"model": "reef_v2_optimus", "slides": []}


@pytest.mark.asyncio
async def test_retrieval_candidates_survive_the_next_chat_turn(agent_db, monkeypatch):
    async def fake_manifest():
        return {"slides": []}

    async def fake_search(*args, **kwargs):
        return {
            "regions": [
                {
                    "candidate_id": "necrosis-candidate-1",
                    "points": [{"x": 100, "y": 200}, {"x": 300, "y": 400}],
                    "score": 0.41,
                }
            ],
            "retrieval_mode": "semantic",
            "warnings": [],
        }

    monkeypatch.setattr(agent, "_load_manifest", fake_manifest)
    monkeypatch.setattr(agent, "search_regions_for_agent", fake_search)
    first_run = agent.AgentRunContext(
        user_sub="user-a", session_id="session-retrieval", context=make_context()
    )
    result = await agent._bedrock_tool(
        "wsi_find_regions",
        {"query": "necrotic tissue", "model": "quiltnet_pmb", "top_k": 1},
        first_run,
    )
    assert result["regions"][0]["candidate_id"] in first_run.retrieval_candidates

    second_run = agent.AgentRunContext(
        user_sub="user-a", session_id="session-retrieval", context=make_context()
    )
    second_run.retrieval_candidates = await agent._load_retrieval_candidates(second_run)
    assert "necrosis-candidate-1" in second_run.retrieval_candidates

    proposal = await agent._bedrock_tool(
        "wsi_propose_annotations",
        {
            "geometry_type": "rectangle",
            "points": [{"x": 0, "y": 0}, {"x": 1, "y": 1}],
            "coordinate_space": "retrieved_candidate",
            "candidate_id": "necrosis-candidate-1",
            "label": "Necrotic tissue",
            "layer_name": "AI research",
            "color": "#ef4444",
            "confidence": 0.7,
            "rationale": "Retrieved candidate for review.",
        },
        second_run,
    )
    action = await agent._get_action(proposal["proposal_id"], "user-a")
    assert action is not None
    assert action.payload["points"] == [
        {"x": 100.0, "y": 160.0},
        {"x": 300.0, "y": 320.0},
    ]


@pytest.fixture
async def agent_db(tmp_path, monkeypatch):
    db_path = tmp_path / "agent.db"
    monkeypatch.setattr(agent.settings, "annotation_database_url", "")
    await agent.init_db(db_path=str(db_path), db_url="")
    await annotation_store.init_db(db_path=str(db_path), db_url="")
    agent._rate_windows.clear()
    yield


@pytest.mark.asyncio
async def test_pending_proposal_requires_single_approval(agent_db):
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-a", context=make_context()
    )
    proposal = await agent._insert_action(
        run_context,
        "viewer_action",
        {"action": "zoom"},
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


def test_bedrock_accepts_standard_environment_credentials(monkeypatch):
    monkeypatch.setattr(agent.settings, "agent_profile", "")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    assert agent._bedrock_configured() is True


def test_bedrock_accepts_task_specific_environment_credentials(monkeypatch):
    monkeypatch.setattr(agent.settings, "agent_profile", "")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("BEDROCK_AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("BEDROCK_AWS_SECRET_ACCESS_KEY", "test-secret-key")
    assert agent._bedrock_configured() is True


def test_bedrock_client_isolated_from_slide_store_endpoint(monkeypatch):
    class FakeSession:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def client(self, service, **kwargs):
            return service, kwargs

    monkeypatch.setattr(agent.boto3, "Session", FakeSession)
    monkeypatch.setattr(agent.settings, "agent_profile", "")
    monkeypatch.setattr(agent.settings, "agent_region", "us-east-1")
    monkeypatch.delenv("BEDROCK_AWS_ENDPOINT_URL", raising=False)
    service, client_kwargs = agent._bedrock_client()
    assert service == "bedrock-runtime"
    assert client_kwargs["endpoint_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com"


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


@pytest.mark.asyncio
async def test_bedrock_stream_emits_text_and_persists_tool_proposal(agent_db, monkeypatch):
    class FakeBedrockClient:
        calls = 0

        def converse(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "output": {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"text": "I found a candidate region."},
                                {
                                    "toolUse": {
                                        "toolUseId": "tool-1",
                                        "name": "wsi_propose_annotations",
                                        "input": {
                                            "geometry_type": "rectangle",
                                            "points": [{"x": 10, "y": 20}, {"x": 30, "y": 40}],
                                            "label": "candidate",
                                            "layer_name": "AI review",
                                            "color": "#7b61ff",
                                            "confidence": 0.8,
                                            "rationale": "The viewport contains a candidate region.",
                                        },
                                    }
                                },
                            ],
                        }
                    }
                }
            return {
                "output": {
                    "message": {
                        "role": "assistant",
                        "content": [{"text": "The proposal is ready for approval."}],
                    }
                }
            }

    fake_client = FakeBedrockClient()
    monkeypatch.setattr(agent.settings, "agent_provider", "bedrock")
    monkeypatch.setattr(agent.settings, "agent_profile", "test-profile")
    monkeypatch.setattr(agent, "_bedrock_configured", lambda: True)
    monkeypatch.setattr(agent, "_bedrock_client", lambda: fake_client)
    request = agent.ChatRequest(
        session_id="session-bedrock", message="Find a candidate", context=make_context()
    )

    events = [chunk async for chunk in agent._stream_agent(request, "user-a")]

    assert any("I found a candidate region." in chunk for chunk in events)
    assert any("The proposal is ready for approval." in chunk for chunk in events)
    proposal_events = [chunk for chunk in events if chunk.startswith("event: proposal")]
    assert len(proposal_events) == 1
    assert "AI review" in proposal_events[0]
    assert events[-1].startswith("event: complete")
    actions = await agent._list_actions("session-bedrock", "user-a", "study-a")
    assert len(actions) == 1
    assert actions[0].status == "pending"


@pytest.mark.asyncio
async def test_bedrock_stream_surfaces_provider_failure_as_error_event(agent_db, monkeypatch):
    class FailingClient:
        def converse(self, **kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(agent.settings, "agent_provider", "bedrock")
    monkeypatch.setattr(agent, "_bedrock_configured", lambda: True)
    monkeypatch.setattr(agent, "_bedrock_client", lambda: FailingClient())
    request = agent.ChatRequest(
        session_id="session-bedrock-error", message="Summarize", context=make_context()
    )

    events = [chunk async for chunk in agent._stream_agent(request, "user-a")]

    assert any(chunk.startswith("event: error") for chunk in events)


@pytest.mark.asyncio
async def test_annotation_proposal_is_canonicalized_to_slide_pixels(agent_db):
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-canonical", context=make_context()
    )
    result = await agent._bedrock_tool(
        "wsi_propose_annotations",
        {
            "geometry_type": "polygon",
            "points": [{"x": 100, "y": 200}, {"x": 300, "y": 200}, {"x": 300, "y": 400}],
            "label": "candidate",
            "layer_name": "Tumor",
            "color": "#ef4444",
            "confidence": 0.8,
            "rationale": "The captured viewport contains a candidate.",
        },
        run_context,
    )
    proposal = (await agent._get_action(result["proposal_id"], "user-a"))
    assert proposal is not None
    assert proposal.payload["geometry_version"] == 2
    assert proposal.payload["coordinate_space"] == "slide_pixels"
    assert proposal.payload["source_fingerprint"] == "source-v2-fingerprint"
    assert proposal.payload["capture_id"] == "capture-1"
    assert proposal.payload["points"] == [
        {"x": 10.0, "y": 16.0},
        {"x": 30.0, "y": 16.0},
        {"x": 30.0, "y": 32.0},
    ]


@pytest.mark.asyncio
async def test_annotation_commit_is_atomic_and_idempotent(agent_db):
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-commit", context=make_context()
    )
    result = await agent._bedrock_tool(
        "wsi_propose_annotations",
        {
            "geometry_type": "rectangle",
            "points": [{"x": 100, "y": 100}, {"x": 300, "y": 300}],
            "label": "candidate",
            "layer_name": "Tumor",
            "color": "#ef4444",
            "confidence": 0.8,
            "rationale": "The captured viewport contains a candidate.",
        },
        run_context,
    )
    request = agent.CommitAnnotationsRequest(
        source_fingerprint="source-v2-fingerprint",
        viewer_generation=3,
        slide_id="slide-a",
    )
    first = await agent._commit_annotations(result["proposal_id"], "user-a", request)
    second = await agent._commit_annotations(result["proposal_id"], "user-a", request)

    assert first.idempotent is False
    assert second.idempotent is True
    assert first.action.status == "completed"
    assert [item.id for item in first.annotations] == [item.id for item in second.annotations]
    assert len(await annotation_store._list_sqlite("slide-a", "study-a", "user-a")) == 1


@pytest.mark.asyncio
async def test_annotation_commit_rejects_stale_source_without_writing(agent_db):
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-stale", context=make_context()
    )
    result = await agent._bedrock_tool(
        "wsi_propose_annotations",
        {
            "geometry_type": "rectangle",
            "points": [{"x": 100, "y": 100}, {"x": 300, "y": 300}],
            "label": "candidate",
            "layer_name": "Tumor",
            "color": "#ef4444",
            "confidence": 0.8,
            "rationale": "The captured viewport contains a candidate.",
        },
        run_context,
    )
    request = agent.CommitAnnotationsRequest(
        source_fingerprint="different-source-v2",
        viewer_generation=3,
        slide_id="slide-a",
    )

    with pytest.raises(HTTPException) as error:
        await agent._commit_annotations(result["proposal_id"], "user-a", request)

    assert error.value.status_code == 409
    assert await annotation_store._list_sqlite("slide-a", "study-a", "user-a") == []
    proposal = await agent._get_action(result["proposal_id"], "user-a")
    assert proposal is not None and proposal.status == "pending"


@pytest.mark.asyncio
async def test_annotation_commit_rolls_back_all_rows_on_failure(agent_db, monkeypatch):
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-rollback", context=make_context()
    )
    result = await agent._bedrock_tool(
        "wsi_propose_annotation_batch",
        {
            "annotations": [
                {
                    "geometry_type": "rectangle",
                    "points": [{"x": 100, "y": 100}, {"x": 200, "y": 200}],
                    "label": "one",
                    "layer_name": "Tumor",
                    "color": "#ef4444",
                    "confidence": 0.8,
                },
                {
                    "geometry_type": "rectangle",
                    "points": [{"x": 300, "y": 300}, {"x": 400, "y": 400}],
                    "label": "two",
                    "layer_name": "Tumor",
                    "color": "#ef4444",
                    "confidence": 0.8,
                },
            ],
            "rationale": "Two candidates in the captured viewport.",
        },
        run_context,
    )
    original = annotation_store._insert_sqlite_connection
    calls = 0

    async def fail_second(db, data, user_sub, annotation_id=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated annotation insert failure")
        return await original(db, data, user_sub, annotation_id)

    monkeypatch.setattr(annotation_store, "_insert_sqlite_connection", fail_second)
    request = agent.CommitAnnotationsRequest(
        source_fingerprint="source-v2-fingerprint",
        viewer_generation=3,
        slide_id="slide-a",
    )
    with pytest.raises(RuntimeError, match="simulated"):
        await agent._commit_annotations(result["proposal_id"], "user-a", request)
    assert await annotation_store._list_sqlite("slide-a", "study-a", "user-a") == []
    proposal = await agent._get_action(result["proposal_id"], "user-a")
    assert proposal is not None and proposal.status == "pending"
