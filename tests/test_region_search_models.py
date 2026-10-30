"""Region search across tile-level models, on both assistant paths.

The KEEP-adapter model's manifest entry carries an internal image ID that only
the retrieval worker may see.  These tests pin that it reaches the worker and
never a client: research API responses, the model's tool output, stored
retrieval runs or SSE events.
"""

import json
import os
from types import SimpleNamespace

import httpx
import pytest
from agents.tool_context import ToolContext

from app import agent, research
from tests.test_agent import SLIDE_A, agent_db, make_context  # noqa: F401

IMAGE_ID = "987654321"
KEEP = research.KEEP_ADAPTER_MODEL


def _manifest(tmp_path, monkeypatch, study_id="study-a", slide_id=SLIDE_A):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "slides": [
                    {
                        "study_id": study_id,
                        "slide_id": slide_id,
                        "slide_width": 1000,
                        "slide_height": 800,
                        "models": [
                            {
                                "id": KEEP,
                                "provider": "keep_adapter",
                                "image_id": IMAGE_ID,
                                "slide_width": 1000,
                                "slide_height": 800,
                                "capabilities": ["text_search"],
                            },
                            {"id": "quiltnet_pmb", "provider": "mussel"},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(research.settings, "research_manifest_uri", str(path))
    monkeypatch.setattr(research.settings, "research_enabled", True)
    monkeypatch.setattr(research.settings, "research_embedding_url", "http://keep-worker")
    monkeypatch.setattr(research.settings, "research_embedding_urls", "")
    monkeypatch.setattr(research.settings, "research_static_fallback", True)
    monkeypatch.setattr(research.settings, "research_region_model", KEEP)
    research._manifest_cache = None
    research._rate_windows.clear()
    return path


class _Worker:
    """Records worker requests and answers like the KEEP worker."""

    def __init__(self, status_code=200, body=None):
        self.requests: list[tuple[str, dict]] = []
        self.status_code = status_code
        self.body = body or {
            "regions": [
                {
                    "candidate_id": "keep-1",
                    "model": KEEP,
                    "points": [{"x": 100, "y": 200}, {"x": 150, "y": 260}],
                    "score": 0.31,
                    "similarity": 0.31,
                    "relative_score": 1.0,
                    "matched_concepts": ["tumor", f"image {IMAGE_ID}"],
                    "verification": "semantic",
                    "rank": 1,
                    "image_id": IMAGE_ID,
                    "shard": f"USC/{IMAGE_ID}.tar",
                },
                {
                    "candidate_id": "keep-2",
                    "points": [{"x": 500, "y": 500}, {"x": 550, "y": 560}],
                    "score": 0.22,
                    "rank": 2,
                },
            ],
            "retrieval_mode": "semantic",
            "warning": "KEEP-space cosine through an adapter; weak on IHC.",
        }

    def install(self, monkeypatch):
        worker = self

        def handler(request: httpx.Request) -> httpx.Response:
            worker.requests.append((str(request.url), json.loads(request.content)))
            return httpx.Response(worker.status_code, json=worker.body)

        real_client = httpx.AsyncClient

        def client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(research.httpx, "AsyncClient", client)
        return self


def test_region_model_defaults_to_quiltnet(monkeypatch):
    monkeypatch.setattr(research.settings, "research_region_model", "quiltnet_pmb")
    assert research.region_model() == "quiltnet_pmb"
    assert research.RegionSearchRequest(query="tumor").model == "quiltnet_pmb"
    monkeypatch.setattr(research.settings, "research_region_model", KEEP)
    assert research.RegionSearchRequest(query="tumor").model == KEEP


def test_query_plan_expands_synonyms_only_for_quiltnet():
    quiltnet = research._plan_for_request("tumor", None, "quiltnet_pmb")
    keep = research._plan_for_request("tumor", None, KEEP)
    assert "neoplasm" in quiltnet["positive"]
    assert keep["positive"] == ["tumor"]


def test_models_default_capabilities_and_hide_private_fields():
    row = {
        "models": [
            {"id": KEEP, "provider": "keep_adapter", "image_id": IMAGE_ID},
            {"id": "quiltnet_pmb", "provider": "mussel"},
        ]
    }
    models = research._models(row)
    assert models[0].capabilities == ["text_search"]
    assert models[1].capabilities == ["text_search", "tile_search"]
    assert IMAGE_ID not in json.dumps([model.model_dump() for model in models])


def test_worker_url_can_be_set_per_model(monkeypatch):
    monkeypatch.setattr(research.settings, "research_embedding_url", "http://quiltnet")
    monkeypatch.setattr(
        research.settings, "research_embedding_urls", f"{KEEP}=http://keep:27710, other=http://x"
    )
    assert research._worker_url(KEEP) == "http://keep:27710"
    assert research._worker_url("quiltnet_pmb") == "http://quiltnet"


async def test_region_search_api_sends_image_id_only_to_the_worker(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    worker = _Worker().install(monkeypatch)
    user = {"sub": "user-a", "study_id": "study-a"}

    models = await research.list_slide_models("study-a", SLIDE_A, user)
    result = await research.search_slide_regions(
        "study-a", SLIDE_A, research.RegionSearchRequest(query="tumor", top_k=5), user
    )

    url, sent = worker.requests[0]
    assert url == "http://keep-worker/v1/search"
    assert sent["model"] == KEEP
    assert sent["model_record"]["image_id"] == IMAGE_ID
    assert (sent["slide_width"], sent["slide_height"]) == (1000, 800)
    assert sent["query_plan"]["positive"] == ["tumor"]

    assert result["model"] == KEEP
    assert result["retrieval_mode"] == "semantic"
    assert [region["candidate_id"] for region in result["regions"]] == ["keep-1", "keep-2"]
    assert result["regions"][0]["matched_concepts"] == ["tumor"]
    assert set(result["regions"][0]) <= {
        "candidate_id", "model", "points", "rank", "score", "similarity",
        "relative_score", "verification", "label", "matched_concepts",
    }
    assert any("weak on IHC" in warning for warning in result["warnings"])
    client_visible = json.dumps([model.model_dump() for model in models]) + json.dumps(result)
    assert IMAGE_ID not in client_visible
    assert "model_record" not in client_visible


async def test_missing_worker_features_fall_back_without_leaking(tmp_path, monkeypatch, caplog):
    _manifest(tmp_path, monkeypatch)
    _Worker(status_code=404, body={"detail": f"no features for {IMAGE_ID}"}).install(monkeypatch)
    result = await research.search_slide_regions(
        "study-a",
        SLIDE_A,
        research.RegionSearchRequest(query="tumor"),
        {"sub": "user-a", "study_id": "study-a"},
    )
    assert result["retrieval_mode"] == "published_label_fallback"
    assert result["regions"] == []
    assert "not available for this slide" in " ".join(result["warnings"])
    assert IMAGE_ID not in json.dumps(result)
    assert IMAGE_ID not in caplog.text


def _tool_context(run_context, name, arguments):
    return ToolContext(
        context=run_context,
        tool_name=name,
        tool_call_id="call-1",
        tool_arguments=json.dumps(arguments),
    )


async def _invoke(tool, run_context, arguments):
    output = await tool.on_invoke_tool(
        _tool_context(run_context, tool.name, arguments), json.dumps(arguments)
    )
    return json.loads(output)


def test_agents_sdk_path_exposes_the_research_tools(monkeypatch):
    monkeypatch.setattr(agent, "_agent_model", lambda: "test-model")
    names = {tool.name for tool in agent._build_agent().tools}
    assert {"wsi_find_regions", "wsi_propose_retrieval_annotations"} <= names
    assert "quiltnet_pmb" not in agent._agent_instructions()


async def test_agents_sdk_tools_search_and_propose(agent_db, tmp_path, monkeypatch):  # noqa: F811
    _manifest(tmp_path, monkeypatch)
    worker = _Worker().install(monkeypatch)
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-keep", context=make_context()
    )

    found = await _invoke(
        agent.wsi_find_regions,
        run_context,
        {"query": "tumor", "positive_concepts": None, "negative_concepts": None, "top_k": 2},
    )
    assert worker.requests[0][1]["model_record"]["image_id"] == IMAGE_ID
    assert found["model"] == KEEP
    assert [region["rank"] for region in found["regions"]] == [1, 2]
    assert IMAGE_ID not in json.dumps(found)

    # A later chat turn proposes from the stored run.
    next_turn = agent.AgentRunContext(
        user_sub="user-a", session_id="session-keep", context=make_context()
    )
    await agent._load_retrieval_state(next_turn)
    stored = next_turn.retrieval_runs[found["retrieval_run_id"]]
    assert stored["model"] == KEEP
    assert IMAGE_ID not in json.dumps(next_turn.retrieval_runs)

    proposed = await _invoke(
        agent.wsi_propose_retrieval_annotations,
        next_turn,
        {
            "retrieval_run_id": found["retrieval_run_id"],
            "ranks": [1],
            "label": "Tumor",
            "layer_name": "AI research",
            "color": "#ef4444",
            "confidence": 0.5,
            "rationale": "Top-ranked region for review.",
        },
    )
    action = await agent._get_action(proposed["proposal_id"], "user-a")
    assert action is not None and action.action_type == "annotation_batch"
    assert action.payload["annotations"][0]["points"] == [
        {"x": 100.0, "y": 160.0},
        {"x": 150.0, "y": 208.0},
    ]
    assert next_turn.proposal_ids == [proposed["proposal_id"]]


async def test_agents_sdk_tool_failure_is_reported_like_bedrock(agent_db):  # noqa: F811
    run_context = agent.AgentRunContext(
        user_sub="user-a", session_id="session-keep", context=make_context()
    )
    result = await _invoke(
        agent.wsi_propose_retrieval_annotations,
        run_context,
        {
            "retrieval_run_id": "missing",
            "ranks": [1],
            "label": "Tumor",
            "layer_name": "AI research",
            "color": "#ef4444",
            "confidence": 0.5,
            "rationale": "x",
        },
    )
    assert result["error"]["code"] == "invalid_tool_request"
    assert run_context.last_tool_error == result["error"]
    assert run_context.tool_errors[0]["name"] == "wsi_propose_retrieval_annotations"


async def test_agents_sdk_stream_emits_proposals_like_bedrock(agent_db, tmp_path, monkeypatch):  # noqa: F811
    _manifest(tmp_path, monkeypatch)
    _Worker().install(monkeypatch)

    class FakeStream:
        def __init__(self, run_context):
            self.run_context = run_context

        async def stream_events(self):
            yield SimpleNamespace(
                type="run_item_stream_event",
                name="tool_called",
                item=SimpleNamespace(raw_item=SimpleNamespace(name="wsi_find_regions")),
            )
            found = await _invoke(
                agent.wsi_find_regions,
                self.run_context,
                {"query": "tumor", "positive_concepts": None, "negative_concepts": None, "top_k": 2},
            )
            yield SimpleNamespace(
                type="run_item_stream_event",
                name="tool_called",
                item=SimpleNamespace(
                    raw_item=SimpleNamespace(name="wsi_propose_retrieval_annotations")
                ),
            )
            await _invoke(
                agent.wsi_propose_retrieval_annotations,
                self.run_context,
                {
                    "retrieval_run_id": found["retrieval_run_id"],
                    "ranks": [1, 2],
                    "label": "Tumor",
                    "layer_name": "AI research",
                    "color": "#ef4444",
                    "confidence": 0.5,
                    "rationale": "Ranked regions for review.",
                },
            )
            yield SimpleNamespace(
                type="raw_response_event",
                data=SimpleNamespace(type="response.output_text.delta", delta="Proposed 2 regions."),
            )

    captured = {}

    def run_streamed(starting_agent, agent_input, *, context, max_turns):
        captured["input"] = agent_input
        return FakeStream(context)

    monkeypatch.setattr(agent.Runner, "run_streamed", run_streamed)
    request = agent.ChatRequest(
        session_id="session-stream", message="Find tumor", context=make_context()
    )
    chunks = [chunk async for chunk in agent._stream_agent(request, "user-a")]
    events = [
        (chunk.split("\n", 1)[0].removeprefix("event: "), json.loads(chunk.split("data: ", 1)[1]))
        for chunk in chunks
    ]
    names = [name for name, _ in events]
    assert names == [
        "tool.called",
        "tool.called",
        "message.delta",
        "proposal",
        "complete",
    ]
    proposal = events[3][1]
    assert proposal["action_type"] == "annotation_batch"
    assert len(proposal["payload"]["annotations"]) == 2
    assert events[-1][1]["success"] is True
    assert events[-1][1]["proposal_ids"] == [proposal["id"]]
    assert IMAGE_ID not in "".join(chunks)
    assert "model_record" not in "".join(chunks)

    # The next turn's prompt lists the stored run without the image ID.
    request = agent.ChatRequest(
        session_id="session-stream", message="Annotate rank 1", context=make_context()
    )
    chunks = [chunk async for chunk in agent._stream_agent(request, "user-a")]
    prompt = captured["input"][0]["content"][0]["text"]
    assert "retrieval_runs" in prompt
    assert IMAGE_ID not in prompt


@pytest.mark.skipif(
    not (os.environ.get("WSI_KEEP_LIVE_URL") and os.environ.get("WSI_KEEP_LIVE_IMAGE_ID")),
    reason="set WSI_KEEP_LIVE_URL, WSI_KEEP_LIVE_IMAGE_ID, WSI_KEEP_LIVE_WIDTH and WSI_KEEP_LIVE_HEIGHT",
)
async def test_live_keep_worker(monkeypatch):
    image_id = os.environ["WSI_KEEP_LIVE_IMAGE_ID"]
    width = int(os.environ["WSI_KEEP_LIVE_WIDTH"])
    height = int(os.environ["WSI_KEEP_LIVE_HEIGHT"])
    monkeypatch.setattr(research.settings, "research_embedding_url", os.environ["WSI_KEEP_LIVE_URL"])
    monkeypatch.setattr(research.settings, "research_embedding_urls", "")
    monkeypatch.setattr(research.settings, "research_static_fallback", False)
    row = {
        "study_id": "study-a",
        "slide_id": SLIDE_A,
        "models": [
            {
                "id": KEEP,
                "provider": "keep_adapter",
                "image_id": image_id,
                "slide_width": width,
                "slide_height": height,
            }
        ],
    }
    result = await research._search_result(
        row,
        research.RegionSearchRequest(query="tumor", model=KEEP, top_k=5),
    )
    assert result["retrieval_mode"] == "semantic"
    assert 1 <= len(result["regions"]) <= 5
    for rank, region in enumerate(result["regions"], start=1):
        assert region["rank"] == rank
        assert all(0 <= point["x"] <= 1000 and 0 <= point["y"] <= 1000 for point in region["points"])
    assert image_id not in json.dumps(result)
