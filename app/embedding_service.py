"""Internal QuiltNet retrieval worker.

This app is intentionally separate from the tile server.  It has the large
text encoder and cached feature tensors, while the public tile API remains
responsible for authorization and manifest scoping.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .quiltnet_retrieval import QuiltNetUnavailable, get_retriever

app = FastAPI(title="WSI QuiltNet retrieval worker")


class SearchRequest(BaseModel):
    model: str = Field(min_length=1, max_length=100)
    model_record: dict[str, Any]
    query_plan: dict[str, Any]
    top_k: int = Field(default=10, ge=1, le=50)
    slide_width: int = Field(gt=0, le=2_000_000)
    slide_height: int = Field(gt=0, le=2_000_000)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "quiltnet-retrieval"}


@app.post("/v1/search")
def search(data: SearchRequest) -> dict[str, Any]:
    try:
        regions = get_retriever().search(
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
