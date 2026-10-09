"""Read-only access to published WSI research assets.

Research assets are prepared offline and published as a study-qualified
manifest.  This service returns references and bounded candidate regions; it
does not expose S3 credentials or let a model choose an arbitrary source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .auth import scoped_user_dependency
from .config import settings
from .query_planner import build_query_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/v1", tags=["wsi-research"])
_READ_USER = Depends(scoped_user_dependency({"research:read"}))
_manifest_lock = asyncio.Lock()
_manifest_cache: tuple[str, float, dict[str, Any]] | None = None
_rate_lock = asyncio.Lock()
_rate_windows: dict[str, list[float]] = {}


class ResearchModel(BaseModel):
    id: str
    provider: str
    version: str | None = None
    features_uri: str | None = None
    coordinates_uri: str | None = None
    thumbnail_uri: str | None = None
    annotations_uri: str | None = None
    native_mpp: float | None = None
    patch_size: float | None = None
    slide_width: int | None = None
    slide_height: int | None = None
    capabilities: list[str] = Field(default_factory=list)


class ResearchSlide(BaseModel):
    study_id: str
    slide_id: str
    patient_id: str | None = None
    sample_id: str | None = None
    models: list[ResearchModel]
    regions: list[dict[str, Any]] = Field(default_factory=list)
    similar_slides: list[dict[str, Any]] = Field(default_factory=list)


class RegionSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    model: str = Field(default="quiltnet_pmb", min_length=1, max_length=100)
    top_k: int = Field(default=10, ge=1, le=50)
    query_plan: dict[str, Any] | None = None
    review_mode: str = Field(default="semantic", pattern=r"^(semantic|review)$")
    slide_width: int | None = Field(default=None, gt=0, le=2_000_000)
    slide_height: int | None = Field(default=None, gt=0, le=2_000_000)


class SimilarSlidesRequest(BaseModel):
    model: str = Field(default="reef_v2_titan", min_length=1, max_length=100)
    top_k: int = Field(default=10, ge=1, le=50)


def _manifest_uri() -> str:
    return str(getattr(settings, "research_manifest_uri", "") or "").strip()


def _read_uri(uri: str) -> tuple[str, float, dict[str, Any]]:
    parsed = urlparse(uri)
    if parsed.scheme in {"", "file"}:
        path = Path(parsed.path if parsed.scheme else uri).expanduser()
        stat = path.stat()
        return str(path), stat.st_mtime, json.loads(path.read_text(encoding="utf-8"))
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ValueError("research manifest must be a local path or s3 URI")
    client = boto3.client(
        "s3",
        region_name=settings.agent_region or None,
        endpoint_url=settings.aws_endpoint_url or None,
    )
    response = client.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
    body = response["Body"].read()
    last_modified = response.get("LastModified")
    stamp = last_modified.timestamp() if last_modified else time.time()
    return uri, stamp, json.loads(body)


async def _load_manifest() -> dict[str, Any]:
    global _manifest_cache
    uri = _manifest_uri()
    if not uri:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WSI research assets are not configured",
        )
    async with _manifest_lock:
        try:
            key, stamp, payload = await asyncio.to_thread(_read_uri, uri)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail="WSI research manifest is unavailable") from exc
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.warning("Unable to read research manifest: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="WSI research manifest is invalid") from exc
        if _manifest_cache and _manifest_cache[0] == key and _manifest_cache[1] == stamp:
            return _manifest_cache[2]
        if isinstance(payload, list):
            payload = {"version": 1, "slides": payload}
        if not isinstance(payload, dict) or not isinstance(payload.get("slides"), list):
            raise HTTPException(status_code=503, detail="WSI research manifest has no slides")
        _manifest_cache = (key, stamp, payload)
        return payload


def _find_slide(payload: dict[str, Any], study_id: str, slide_id: str) -> dict[str, Any]:
    for row in payload["slides"]:
        if not isinstance(row, dict):
            continue
        if str(row.get("study_id", "")) == study_id and str(row.get("slide_id", "")) == slide_id:
            return row
    raise HTTPException(status_code=404, detail="Research assets are not published for this slide")


def _models(row: dict[str, Any]) -> list[ResearchModel]:
    raw_models = row.get("models", [])
    if isinstance(raw_models, dict):
        raw_models = [dict(value, id=key) if isinstance(value, dict) else {"id": key} for key, value in raw_models.items()]
    result = []
    for model in raw_models:
        if not isinstance(model, dict) or not model.get("id"):
            continue
        value = dict(model)
        if not value.get("capabilities") and value.get("id") == "quiltnet_pmb":
            value["capabilities"] = ["text_search", "tile_search"]
        result.append(ResearchModel(**value))
    return result


def _model_record(row: dict[str, Any], model_id: str) -> dict[str, Any]:
    raw_models = row.get("models", [])
    if isinstance(raw_models, dict):
        raw_models = [dict(value, id=key) if isinstance(value, dict) else {"id": key} for key, value in raw_models.items()]
    return next(
        (
            model
            for model in raw_models
            if isinstance(model, dict) and str(model.get("id")) == model_id
        ),
        {},
    )


def _plan_for_request(query: str, supplied: dict[str, Any] | None) -> dict[str, Any]:
    if supplied is None:
        return build_query_plan(query)
    positive = supplied.get("positive")
    negative = supplied.get("negative")
    if not isinstance(positive, list) or not isinstance(negative, list):
        return build_query_plan(query)
    clean_positive = [str(item).strip() for item in positive[:4] if str(item).strip()]
    clean_negative = [str(item).strip() for item in negative[:2] if str(item).strip()]
    if not clean_positive:
        return build_query_plan(query)
    return {
        "primary": str(supplied.get("primary") or clean_positive[0]),
        "positive": clean_positive,
        "negative": clean_negative,
        "summary": str(supplied.get("summary") or "Searching for " + clean_positive[0]),
    }


async def _check_rate_limit(user: dict[str, Any]) -> None:
    sub = str(user.get("sub", "anonymous"))
    now = time.monotonic()
    async with _rate_lock:
        values = _rate_windows.setdefault(sub, [])
        values[:] = [stamp for stamp in values if stamp > now - 60]
        if len(values) >= max(1, settings.research_rate_limit_per_minute):
            raise HTTPException(status_code=429, detail="Research retrieval rate limit exceeded", headers={"Retry-After": "60"})
        values.append(now)


def _require_study(user: dict[str, Any], study_id: str) -> None:
    if user.get("study_id") and user["study_id"] != study_id:
        raise HTTPException(status_code=403, detail="Token study scope does not match request")


def _region_search_text(region: dict[str, Any]) -> str:
    searchable = []
    for key, value in region.items():
        if key in {"points", "source_uri", "model", "score", "rank"}:
            continue
        if isinstance(value, str):
            searchable.append(value)
        elif isinstance(value, list):
            searchable.extend(item for item in value if isinstance(item, str))
    return " ".join(searchable).casefold()


def _query_terms(query: str) -> list[str]:
    return re.findall(r"[\w-]+", query.casefold())


def _matches_region_query(region: dict[str, Any], query: str) -> bool:
    terms = _query_terms(query)
    if not terms:
        return True
    text = _region_search_text(region)
    return bool(text) and all(term in text for term in terms)


def _bounded_regions(
    row: dict[str, Any], model: str, top_k: int, query: str = ""
) -> list[dict[str, Any]]:
    regions = row.get("regions", [])
    if isinstance(regions, dict):
        regions = regions.get(model, [])
    selected = []
    for rank, region in enumerate(regions):
        if not isinstance(region, dict):
            continue
        if region.get("model") not in (None, model):
            continue
        if not _matches_region_query(region, query):
            continue
        points = region.get("points")
        if not isinstance(points, list) or not points:
            continue
        clean_points = []
        for point in points[:100]:
            if not isinstance(point, dict):
                continue
            x, y = point.get("x"), point.get("y")
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (x, y)):
                continue
            clean_points.append({"x": max(0.0, min(1000.0, float(x))), "y": max(0.0, min(1000.0, float(y)))})
        if len(clean_points) < 2:
            continue
        selected.append({**{key: value for key, value in region.items() if key not in {"points", "source_uri"}}, "points": clean_points, "rank": rank + 1})
        if len(selected) >= top_k:
            break
    return selected


async def _semantic_regions(
    row: dict[str, Any],
    model: str,
    top_k: int,
    query_plan: dict[str, Any],
    slide_width: int | None,
    slide_height: int | None,
) -> list[dict[str, Any]]:
    model_record = _model_record(row, model)
    width = slide_width or model_record.get("slide_width") or row.get("slide_width")
    height = slide_height or model_record.get("slide_height") or row.get("slide_height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise RuntimeError("Slide dimensions are required for semantic research search")
    payload = {
        "model": model,
        "model_record": model_record,
        "query_plan": query_plan,
        "top_k": top_k,
        "slide_width": width,
        "slide_height": height,
    }
    endpoint = str(getattr(settings, "research_embedding_url", "") or "").strip()
    if endpoint:
        url = endpoint.rstrip("/") + "/v1/search"
        try:
            async with httpx.AsyncClient(timeout=settings.research_embedding_timeout_seconds) as client:
                response = await client.post(url, json=payload)
            response.raise_for_status()
            result = response.json()
            regions = result.get("regions") if isinstance(result, dict) else None
            if not isinstance(regions, list):
                raise RuntimeError("QuiltNet retrieval worker returned no regions")
            return [region for region in regions if isinstance(region, dict)]
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("QuiltNet retrieval worker is unavailable") from exc
    if bool(getattr(settings, "research_embedding_in_process", False)):
        try:
            from .quiltnet_retrieval import get_retriever

            return await asyncio.to_thread(
                get_retriever().search,
                model_id=model,
                model_record=model_record,
                query_plan=query_plan,
                top_k=top_k,
                slide_width=width,
                slide_height=height,
            )
        except Exception as exc:
            raise RuntimeError("In-process QuiltNet retrieval is unavailable") from exc
    raise RuntimeError("QuiltNet retrieval worker is not configured")


async def _search_result(
    row: dict[str, Any],
    data: RegionSearchRequest,
) -> dict[str, Any]:
    query_plan = _plan_for_request(data.query, data.query_plan)
    warnings: list[str] = []
    try:
        regions = await _semantic_regions(
            row,
            data.model,
            data.top_k,
            query_plan,
            data.slide_width,
            data.slide_height,
        )
        retrieval_mode = "semantic"
        warnings.append(
            "Similarity is a research ranking, not a diagnostic confidence score."
        )
    except RuntimeError as exc:
        if not settings.research_static_fallback:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        regions = _bounded_regions(row, data.model, data.top_k, data.query)
        retrieval_mode = "published_label_fallback"
        warnings.append(
            "QuiltNet semantic retrieval is unavailable; showing only published label matches."
        )
        logger.warning("Semantic research search fell back to published labels: %s", exc)
    return {
        "regions": regions,
        "query_plan": query_plan,
        "retrieval_mode": retrieval_mode,
        "review_mode": data.review_mode,
        "warnings": warnings,
    }


@router.get("/health")
async def research_health() -> dict[str, Any]:
    return {
        "enabled": bool(settings.research_enabled),
        "configured": bool(_manifest_uri()),
        "semantic_configured": bool(
            getattr(settings, "research_embedding_url", "")
            or getattr(settings, "research_embedding_in_process", False)
        ),
    }


@router.get("/slides/{study_id}/{slide_id}/models", response_model=list[ResearchModel])
async def list_slide_models(
    study_id: str,
    slide_id: str,
    user: dict[str, Any] = _READ_USER,
) -> list[ResearchModel]:
    _require_study(user, study_id)
    if not settings.research_enabled:
        raise HTTPException(status_code=404, detail="WSI research assets are disabled")
    row = _find_slide(await _load_manifest(), study_id, slide_id)
    return _models(row)


@router.post("/slides/{study_id}/{slide_id}/region-search")
async def search_slide_regions(
    study_id: str,
    slide_id: str,
    data: RegionSearchRequest,
    user: dict[str, Any] = _READ_USER,
) -> dict[str, Any]:
    _require_study(user, study_id)
    if not settings.research_enabled:
        raise HTTPException(status_code=404, detail="WSI research assets are disabled")
    await _check_rate_limit(user)
    row = _find_slide(await _load_manifest(), study_id, slide_id)
    models = {model.id for model in _models(row)}
    if data.model not in models:
        raise HTTPException(status_code=404, detail="Requested research model is unavailable for this slide")
    result = await _search_result(row, data)
    return {
        "study_id": study_id,
        "slide_id": slide_id,
        "model": data.model,
        "query": data.query,
        **result,
    }


@router.post("/slides/{study_id}/{slide_id}/similar-slides")
async def similar_slides(
    study_id: str,
    slide_id: str,
    data: SimilarSlidesRequest,
    user: dict[str, Any] = _READ_USER,
) -> dict[str, Any]:
    _require_study(user, study_id)
    if not settings.research_enabled:
        raise HTTPException(status_code=404, detail="WSI research assets are disabled")
    await _check_rate_limit(user)
    row = _find_slide(await _load_manifest(), study_id, slide_id)
    models = {model.id for model in _models(row)}
    if data.model not in models:
        raise HTTPException(status_code=404, detail="Requested research model is unavailable for this slide")
    return {
        "study_id": study_id,
        "slide_id": slide_id,
        "model": data.model,
        "slides": _similar_slides_for_model(row, data.model, data.top_k),
    }


def _similar_slides_for_model(
    row: dict[str, Any], model: str, top_k: int
) -> list[dict[str, Any]]:
    """Return only candidates produced by the requested embedding model."""
    candidates = row.get("similar_slides", [])
    if isinstance(candidates, dict):
        candidates = candidates.get(model, [])
    if not isinstance(candidates, list):
        return []
    return [
        candidate
        for candidate in candidates[:top_k]
        if isinstance(candidate, dict)
        and candidate.get("model") in (None, model)
    ]


def _mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "wsi_find_regions",
            "description": "Find published model-ranked regions for an authorized WSI slide.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string"},
                    "slide_id": {"type": "string"},
                    "model": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["study_id", "slide_id", "model"],
            },
        },
        {
            "name": "wsi_find_similar_slides",
            "description": "Find published slides similar to an authorized WSI slide.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "study_id": {"type": "string"},
                    "slide_id": {"type": "string"},
                    "model": {"type": "string"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["study_id", "slide_id", "model"],
            },
        },
    ]


@router.post("/mcp")
async def research_mcp(
    request: Request,
    user: dict[str, Any] = _READ_USER,
) -> Response:
    """Small streamable-HTTP MCP surface for cBioAgent's read-only WSI tools."""
    if not settings.research_enabled:
        raise HTTPException(status_code=404, detail="WSI research assets are disabled")
    message = await request.json()
    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "initialize":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-03-26"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cbioportal-wsi-research", "version": "1.0"},
                },
            }
        )
    if method == "tools/list":
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": _mcp_tools()}})
    if method != "tools/call":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}},
            status_code=400,
        )
    params = message.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    await _check_rate_limit(user)
    study_id = str(arguments.get("study_id", ""))
    slide_id = str(arguments.get("slide_id", ""))
    _require_study(user, study_id)
    manifest = await _load_manifest()
    row = _find_slide(manifest, study_id, slide_id)
    model = str(arguments.get("model", ""))
    top_k = min(10, max(1, int(arguments.get("top_k", 5))))
    if model not in {item.id for item in _models(row)}:
        result: dict[str, Any] = {"error": "Requested research model is unavailable"}
    elif name == "wsi_find_regions":
        result = {"regions": _bounded_regions(row, model, top_k), "normalized_coordinate_space": "0..1000"}
    elif name == "wsi_find_similar_slides":
        result = {"slides": _similar_slides_for_model(row, model, top_k)}
    else:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Unknown WSI research tool"}},
            status_code=400,
        )
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result},
        }
    )


def find_regions_for_agent(
    manifest: dict[str, Any],
    study_id: str,
    slide_id: str,
    model: str,
    top_k: int,
    query: str = "",
) -> list[dict[str, Any]]:
    """Bounded in-process lookup used by the Bedrock tool loop."""
    row = _find_slide(manifest, study_id, slide_id)
    if model not in {item.id for item in _models(row)}:
        return []
    return _bounded_regions(row, model, top_k, query)


async def search_regions_for_agent(
    manifest: dict[str, Any],
    study_id: str,
    slide_id: str,
    model: str,
    top_k: int,
    query: str,
    slide_width: int,
    slide_height: int,
    query_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the same semantic search used by the direct viewer control."""
    row = _find_slide(manifest, study_id, slide_id)
    if model not in {item.id for item in _models(row)}:
        return {"regions": [], "query_plan": _plan_for_request(query, query_plan), "retrieval_mode": "none", "warnings": []}
    data = RegionSearchRequest(
        query=query or "slide regions",
        model=model,
        top_k=top_k,
        query_plan=query_plan,
        slide_width=slide_width,
        slide_height=slide_height,
    )
    return await _search_result(row, data)
