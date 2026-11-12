"""Read-only access to published WSI research assets.

Research assets are prepared offline and published as a study-qualified
manifest.  This service returns references and bounded candidate regions; it
does not expose S3 credentials or let a model choose an arbitrary source.
"""

from __future__ import annotations

import asyncio
import hashlib
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

from .request_identity import require_same_study, require_user
from .config import settings
from .query_planner import build_query_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research/v1", tags=["wsi-research"])
# The proxy-asserted user and study (app.request_identity).
_READ_USER = Depends(require_user)
_manifest_lock = asyncio.Lock()
_manifest_cache: tuple[str, float, dict[str, Any]] | None = None
_rate_lock = asyncio.Lock()
_rate_windows: dict[str, list[float]] = {}

QUILTNET_MODEL = "quiltnet_pmb"
KEEP_ADAPTER_MODEL = "reef_v2_hoptimus1_keep"
# Tile-level models that answer text-to-region search, with the capabilities
# a manifest entry gets when it lists none.
REGION_SEARCH_MODELS: dict[str, list[str]] = {
    QUILTNET_MODEL: ["text_search", "tile_search"],
    KEEP_ADAPTER_MODEL: ["text_search"],
}
# Models the tile server can score itself when no worker URL is configured.
_IN_PROCESS_MODELS = frozenset({QUILTNET_MODEL})
# Manifest model-record fields that are internal identifiers.  They travel to
# the retrieval worker only and are scrubbed from anything a worker returns.
_PRIVATE_MODEL_FIELDS = ("image_id",)
_REGION_TEXT_LIMIT = 200
_RETRIEVAL_MODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def region_model() -> str:
    """The configured tile-level model for text-to-region search."""
    return str(getattr(settings, "research_region_model", "") or "").strip() or QUILTNET_MODEL


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
    model: str = Field(default_factory=region_model, min_length=1, max_length=100)
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
        if not value.get("capabilities") and value.get("id") in REGION_SEARCH_MODELS:
            value["capabilities"] = list(REGION_SEARCH_MODELS[value["id"]])
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


def _plan_for_request(
    query: str, supplied: dict[str, Any] | None, model: str = QUILTNET_MODEL
) -> dict[str, Any]:
    # The synonym expansions are tuned to the QuiltNet text vocabulary; other
    # models receive the user's own concepts.
    expand = model == QUILTNET_MODEL
    if supplied is None:
        return build_query_plan(query, expand=expand)
    positive = supplied.get("positive")
    negative = supplied.get("negative")
    if not isinstance(positive, list) or not isinstance(negative, list):
        return build_query_plan(query, expand=expand)
    clean_positive = [str(item).strip() for item in positive[:4] if str(item).strip()]
    clean_negative = [str(item).strip() for item in negative[:2] if str(item).strip()]
    if not clean_positive:
        return build_query_plan(query, expand=expand)
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
    require_same_study(user, study_id, "the research request's study")


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


def _worker_url(model: str) -> str:
    """The retrieval worker base URL for ``model`` ('' when none is set)."""
    for item in str(getattr(settings, "research_embedding_urls", "") or "").split(","):
        name, separator, url = item.partition("=")
        if separator and name.strip() == model and url.strip():
            return url.strip()
    return str(getattr(settings, "research_embedding_url", "") or "").strip()


def _private_values(model_record: dict[str, Any]) -> set[str]:
    return {
        str(model_record[name]).strip()
        for name in _PRIVATE_MODEL_FIELDS
        if str(model_record.get(name) or "").strip()
    }


def _public_text(value: Any, private: set[str], limit: int = _REGION_TEXT_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:limit]
    if not text or any(secret in text for secret in private):
        return None
    return text


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _worker_regions(
    regions: list[Any], model: str, model_record: dict[str, Any], top_k: int
) -> list[dict[str, Any]]:
    """Keep only the documented region fields a worker returns.

    Regions reach the assistant, its stored retrieval runs and API clients, so
    unknown fields are dropped and no text may carry a private model-record
    value.
    """
    private = _private_values(model_record)
    selected: list[dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        points = region.get("points")
        if not isinstance(points, list):
            continue
        clean_points = []
        for point in points[:100]:
            if not isinstance(point, dict):
                continue
            x, y = _finite_number(point.get("x")), _finite_number(point.get("y"))
            if x is None or y is None:
                continue
            clean_points.append({"x": max(0.0, min(1000.0, x)), "y": max(0.0, min(1000.0, y))})
        if len(clean_points) < 2:
            continue
        candidate_id = _public_text(region.get("candidate_id"), private, 128)
        if candidate_id is None:
            candidate_id = hashlib.sha1(
                json.dumps([model, clean_points], separators=(",", ":")).encode()
            ).hexdigest()[:16]
        clean: dict[str, Any] = {
            "candidate_id": candidate_id,
            "model": model,
            "points": clean_points,
            "rank": len(selected) + 1,
        }
        for name in ("score", "similarity", "relative_score"):
            number = _finite_number(region.get(name))
            if number is not None:
                clean[name] = number
        verification = _public_text(region.get("verification"), private, 64)
        if verification is not None:
            clean["verification"] = verification
        label = _public_text(region.get("label"), private)
        if label is not None:
            clean["label"] = label
        concepts = region.get("matched_concepts")
        if isinstance(concepts, list):
            clean["matched_concepts"] = [
                text
                for text in (_public_text(item, private) for item in concepts[:8])
                if text is not None
            ]
        selected.append(clean)
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
) -> dict[str, Any]:
    """Ask the model's retrieval worker for ranked regions.

    Returns ``{"regions", "retrieval_mode", "warnings"}`` with sanitized
    regions; raises RuntimeError when semantic search is unavailable.
    """
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
    endpoint = _worker_url(model)
    if endpoint:
        url = endpoint.rstrip("/") + "/v1/search"
        try:
            async with httpx.AsyncClient(timeout=settings.research_embedding_timeout_seconds) as client:
                response = await client.post(url, json=payload)
            if response.status_code == 404:
                raise RuntimeError("Region-search features are not available for this slide")
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("The region retrieval worker is unavailable") from exc
        regions = result.get("regions") if isinstance(result, dict) else None
        if not isinstance(regions, list):
            raise RuntimeError("The region retrieval worker returned no regions")
        private = _private_values(model_record)
        mode = result.get("retrieval_mode")
        warning = _public_text(result.get("warning"), private, 500)
        return {
            "regions": _worker_regions(regions, model, model_record, top_k),
            "retrieval_mode": mode if isinstance(mode, str) and _RETRIEVAL_MODE.fullmatch(mode) else "semantic",
            "warnings": [warning] if warning else [],
        }
    if model in _IN_PROCESS_MODELS and bool(getattr(settings, "research_embedding_in_process", False)):
        try:
            from .quiltnet_retrieval import get_retriever

            regions = await asyncio.to_thread(
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
        return {
            "regions": _worker_regions(regions, model, model_record, top_k),
            "retrieval_mode": "semantic",
            "warnings": [],
        }
    raise RuntimeError("The region retrieval worker is not configured")


async def _search_result(
    row: dict[str, Any],
    data: RegionSearchRequest,
) -> dict[str, Any]:
    query_plan = _plan_for_request(data.query, data.query_plan, data.model)
    warnings: list[str] = []
    try:
        semantic = await _semantic_regions(
            row,
            data.model,
            data.top_k,
            query_plan,
            data.slide_width,
            data.slide_height,
        )
        regions = semantic["regions"]
        retrieval_mode = semantic["retrieval_mode"]
        warnings.append(
            "Similarity is a research ranking, not a diagnostic confidence score."
        )
        warnings.extend(semantic["warnings"])
    except RuntimeError as exc:
        if not settings.research_static_fallback:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        regions = _bounded_regions(row, data.model, data.top_k, data.query)
        retrieval_mode = "published_label_fallback"
        warnings.append(
            f"Semantic region retrieval is unavailable ({exc}); showing only published label matches."
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
        "region_model": region_model(),
        "semantic_configured": bool(
            _worker_url(region_model())
            or (
                region_model() in _IN_PROCESS_MODELS
                and getattr(settings, "research_embedding_in_process", False)
            )
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
    try:
        row = _find_slide(manifest, study_id, slide_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        row = None
    if row is None or model not in {item.id for item in _models(row)}:
        return {
            "regions": [],
            "query_plan": _plan_for_request(query, query_plan, model),
            "retrieval_mode": "none",
            "warnings": ["Region search is not published for this slide."],
        }
    data = RegionSearchRequest(
        query=query or "slide regions",
        model=model,
        top_k=top_k,
        query_plan=query_plan,
        slide_width=slide_width,
        slide_height=slide_height,
    )
    return await _search_result(row, data)
