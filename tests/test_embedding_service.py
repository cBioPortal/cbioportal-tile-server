import asyncio

import pytest
from fastapi import Response

from app import embedding_service


def test_ready_reports_unavailable_model(monkeypatch):
    embedding_service.app.state.ready = False
    embedding_service.app.state.ready_error = "model is warming"
    response = Response()

    result = embedding_service.ready(response)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert result["status"] == "starting"


@pytest.mark.asyncio
async def test_search_rejects_requests_beyond_bounded_queue(monkeypatch):
    monkeypatch.setattr(embedding_service.settings, "quiltnet_max_concurrent_searches", 1)
    monkeypatch.setattr(embedding_service.settings, "quiltnet_max_queue", 2)
    embedding_service.app.state.queue_lock = asyncio.Lock()
    embedding_service.app.state.search_semaphore = asyncio.Semaphore(1)
    embedding_service.app.state.queued_requests = 3
    embedding_service.app.state.ready = True

    request = embedding_service.SearchRequest(
        model="quiltnet_pmb",
        model_record={},
        query_plan={"primary": "tumor"},
        slide_width=100,
        slide_height=100,
    )

    with pytest.raises(embedding_service.HTTPException) as error:
        await embedding_service.search(request)

    assert error.value.status_code == 503
    assert error.value.headers["Retry-After"] == "5"


@pytest.mark.asyncio
async def test_startup_warmup_sets_readiness(monkeypatch):
    monkeypatch.setattr(embedding_service, "warm_model", lambda: None)
    await embedding_service.start_warmup()
    await embedding_service.app.state.warmup_task

    assert embedding_service.app.state.ready is True
    assert embedding_service.app.state.ready_error == ""
