import json

import pytest
from fastapi import HTTPException

from app import research


@pytest.fixture
def local_manifest(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "study_id": "study-a",
                "slides": [
                    {
                        "study_id": "study-a",
                        "slide_id": "slide-a",
                        "models": [
                            {"id": "quiltnet_pmb", "provider": "mussel", "version": "test"}
                        ],
                        "regions": [
                            {
                                "model": "quiltnet_pmb",
                                "label": "candidate",
                                "source_uri": "s3://private/object",
                                "points": [{"x": -10, "y": 20}, {"x": 1200, "y": 40}],
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(research.settings, "research_manifest_uri", str(path))
    monkeypatch.setattr(research.settings, "research_enabled", True)
    research._manifest_cache = None
    research._rate_windows.clear()
    return path


@pytest.mark.asyncio
async def test_research_manifest_is_study_scoped_and_regions_are_bounded(local_manifest):
    manifest = await research._load_manifest()
    models = research._models(research._find_slide(manifest, "study-a", "slide-a"))
    assert [model.id for model in models] == ["quiltnet_pmb"]

    regions = research.find_regions_for_agent(
        manifest, "study-a", "slide-a", "quiltnet_pmb", 10, "candidate"
    )
    assert regions[0]["points"] == [{"x": 0.0, "y": 20.0}, {"x": 1000.0, "y": 40.0}]
    assert "source_uri" not in regions[0]

    assert (
        research.find_regions_for_agent(
            manifest, "study-a", "slide-a", "quiltnet_pmb", 10, "not published"
        )
        == []
    )

    with pytest.raises(HTTPException) as error:
        research.find_regions_for_agent(
            manifest, "other-study", "slide-a", "quiltnet_pmb", 10
        )
    assert error.value.status_code == 404


def test_research_mcp_tools_are_read_only():
    tools = {tool["name"] for tool in research._mcp_tools()}
    assert tools == {"wsi_find_regions", "wsi_find_similar_slides"}


def test_similar_slides_are_scoped_to_the_requested_model():
    row = {
        "similar_slides": [
            {"slide_id": "titan-slide", "model": "reef_v2_titan"},
            {"slide_id": "optimus-slide", "model": "reef_v2_optimus"},
            {"slide_id": "untyped-slide"},
        ]
    }

    assert research._similar_slides_for_model(row, "reef_v2_titan", 10) == [
        {"slide_id": "titan-slide", "model": "reef_v2_titan"},
        {"slide_id": "untyped-slide"},
    ]
    assert research._similar_slides_for_model(row, "reef_v2_optimus", 10) == [
        {"slide_id": "optimus-slide", "model": "reef_v2_optimus"},
        {"slide_id": "untyped-slide"},
    ]


@pytest.mark.asyncio
async def test_region_search_returns_interpreted_plan_and_labels_fallback(local_manifest):
    research.settings.research_embedding_url = ""
    research.settings.research_embedding_in_process = False
    research.settings.research_static_fallback = True
    row = research._find_slide(await research._load_manifest(), "study-a", "slide-a")
    result = await research._search_result(
        row,
        research.RegionSearchRequest(
            query="carcinoma without necrosis",
            model="quiltnet_pmb",
            top_k=10,
        ),
    )
    assert result["retrieval_mode"] == "published_label_fallback"
    assert result["query_plan"]["positive"]
    assert "necrosis" in result["query_plan"]["negative"]
    assert result["warnings"]


@pytest.mark.asyncio
async def test_semantic_search_uses_worker_response(local_manifest, monkeypatch):
    research.settings.research_embedding_url = "http://embedding-worker"
    row = research._find_slide(await research._load_manifest(), "study-a", "slide-a")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"regions": [{"candidate_id": "tile-1", "points": []}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            assert url == "http://embedding-worker/v1/search"
            assert json["query_plan"]["positive"]
            assert json["slide_width"] == 1000
            return FakeResponse()

    monkeypatch.setattr(research.httpx, "AsyncClient", FakeClient)
    result = await research._search_result(
        row,
        research.RegionSearchRequest(
            query="carcinoma",
            model="quiltnet_pmb",
            slide_width=1000,
            slide_height=1000,
        ),
    )
    assert result["retrieval_mode"] == "semantic"
    assert result["regions"][0]["candidate_id"] == "tile-1"
