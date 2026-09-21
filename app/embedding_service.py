"""Internal QuiltNet retrieval worker.

This app is intentionally separate from the tile server.  It has the large
text encoder and cached feature tensors, while the public tile API remains
responsible for authorization and manifest scoping.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from .config import settings
from .quiltnet_retrieval import (
    QuiltNetUnavailable,
    get_retriever,
    runtime_info,
    warm_model,
)

app = FastAPI(title="WSI QuiltNet retrieval worker")
logger = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    model_record: dict[str, Any]
    query_plan: dict[str, Any]
    top_k: int = Field(default=10, ge=1, le=50)
    slide_width: int = Field(gt=0, le=2_000_000)
    slide_height: int = Field(gt=0, le=2_000_000)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "quiltnet-retrieval", **runtime_info()}


@app.get("/ready")
def ready(response: Response) -> dict[str, Any]:
    if not getattr(app.state, "ready", False):
        response.status_code = 503
        response.headers["Retry-After"] = "5"
        return {
            "status": "starting",
            "service": "quiltnet-retrieval",
            "detail": getattr(app.state, "ready_error", "model is warming"),
            **runtime_info(),
        }
    return {"status": "ready", "service": "quiltnet-retrieval", **runtime_info()}


@app.on_event("startup")
async def start_warmup() -> None:
    app.state.ready = False
    app.state.ready_error = "model is warming"
    app.state.search_semaphore = asyncio.Semaphore(
        max(1, settings.quiltnet_max_concurrent_searches)
    )
    app.state.queue_lock = asyncio.Lock()
    app.state.queued_requests = 0

    async def warmup() -> None:
        try:
            await asyncio.to_thread(warm_model)
        except Exception as exc:
            app.state.ready_error = "QuiltNet model failed to load"
            logger.exception("QuiltNet readiness warmup failed: %s", exc)
        else:
            app.state.ready = True
            app.state.ready_error = ""

    app.state.warmup_task = asyncio.create_task(warmup())


@app.post("/v1/search")
async def search(data: SearchRequest) -> dict[str, Any]:
    if not getattr(app.state, "ready", False):
        raise HTTPException(
            status_code=503,
            detail=getattr(app.state, "ready_error", "QuiltNet worker is not ready"),
            headers={"Retry-After": "5"},
        )
    queue_limit = max(
        1,
        settings.quiltnet_max_concurrent_searches + settings.quiltnet_max_queue,
    )
    async with app.state.queue_lock:
        if app.state.queued_requests >= queue_limit:
            raise HTTPException(
                status_code=503,
                detail="QuiltNet retrieval queue is full",
                headers={"Retry-After": "5"},
            )
        app.state.queued_requests += 1
    acquired = False
    try:
        try:
            await asyncio.wait_for(
                app.state.search_semaphore.acquire(),
                timeout=max(0.1, settings.quiltnet_queue_timeout_seconds),
            )
            acquired = True
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=503,
                detail="QuiltNet retrieval queue timed out",
                headers={"Retry-After": "5"},
            ) from exc
        try:
            regions = await asyncio.to_thread(
                get_retriever().search,
                model_id=data.model,
                model_record=data.model_record,
                query_plan=data.query_plan,
                top_k=data.top_k,
                slide_width=data.slide_width,
                slide_height=data.slide_height,
            )
        except QuiltNetUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "regions": regions,
            "retrieval_mode": "semantic",
            "warning": "Similarity indicates model relevance, not clinical confidence.",
        }
    finally:
        if acquired:
            app.state.search_semaphore.release()
        async with app.state.queue_lock:
            app.state.queued_requests -= 1
