"""
Tile server — FastAPI application.

Endpoints:
  GET /health
  GET /tiles/{slide_id}/metadata
  GET /tiles/{slide_id}/thumbnail?width=256&height=256
  GET /tiles/{slide_id}/zxy/{z}/{x}/{y}

All tile and thumbnail responses carry long-lived Cache-Control headers so a
CDN or nginx proxy_cache can absorb the bulk of repeat requests.
"""

import asyncio
import json
import logging
from collections import OrderedDict
from contextlib import asynccontextmanager

# Ensure app.* loggers emit to stderr alongside uvicorn's own loggers.
# uvicorn's dictConfig only configures uvicorn.* — root logger has no handler
# by default, so INFO from app.* would be silently dropped without this.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from . import cache as tile_cache
from . import meta
from .auth import InvalidWsiToken, validate_wsi_token
from .config import settings
from .metrics import (
    CACHE_MISS_LEADERS,
    COALESCED_CACHE_MISS_REQUESTS,
    DECODE_SOURCE_PIXELS,
    OVERSIZED_DECODE_REJECTIONS,
    metrics_payload,
    track_image_operation,
)
from .meta import get_slide_dbmeta, search_suggestions
from .rate_limit import EXPENSIVE_PATH_PREFIXES, rate_limiter
from .resource_index import ResourceIndexUnavailable, get_resource_index
from .slides import SlideCache
from .tiles import OverviewTooLarge, get_thumbnail_bytes, get_tile_bytes, render_tile_image, slide_metadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_slides: SlideCache | None = None
# In-process cache: image_id → s3 URI (populated on first open, survives across requests)
_path_cache: OrderedDict[str, str] = OrderedDict()
_image_operation_semaphore: asyncio.Semaphore | None = None


class _SingleFlight:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._futures: dict[str, asyncio.Future] = {}

    async def do(self, key: str, kind: str, producer):
        async with self._lock:
            future = self._futures.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._futures[key] = future
                leader = True
                CACHE_MISS_LEADERS.labels(kind=kind).inc()
            else:
                leader = False
                COALESCED_CACHE_MISS_REQUESTS.labels(kind=kind).inc()

        if not leader:
            return await future

        try:
            result = await producer()
        except Exception as exc:
            future.set_exception(exc)
            future.exception()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            async with self._lock:
                self._futures.pop(key, None)


_singleflight = _SingleFlight()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _slides, _image_operation_semaphore
    _slides = SlideCache(capacity=settings.max_open_slides)
    _image_operation_semaphore = asyncio.Semaphore(settings.max_image_operations)
    await tile_cache.init_cache()
    if not settings.aws_endpoint_url:
        logger.warning(
            "AWS_ENDPOINT_URL is not set — S3 requests will go to public AWS "
            "(set this to your Dell ECS endpoint in production)"
        )
    logger.info(
        "Tile server ready. max_open_slides=%d endpoint=%s",
        settings.max_open_slides,
        settings.aws_endpoint_url or "AWS default",
    )
    yield
    _slides.close_all()
    await tile_cache.close_cache()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WSI Tile Server",
    description="Serve SVS whole-slide image tiles directly from Dell ECS (S3) via tiffslide.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def limit_expensive_requests(request: Request, call_next):
    path = request.scope["path"]
    if (
        settings.wsi_auth_required
        and path.startswith(EXPENSIVE_PATH_PREFIXES)
        and (payload := getattr(request.state, "wsi_token_payload", None)) is not None
        and not rate_limiter.allow(
            payload["sub"],
            settings.rate_limit_per_minute,
        )
    ):
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content="Rate limit exceeded",
        )
    return await call_next(request)


@app.middleware("http")
async def require_wsi_capability(request: Request, call_next):
    """Require a cBioPortal-issued capability for every non-health API request."""
    if request.scope["path"] in ("/health", "/wsi/health"):
        return await call_next(request)
    if not settings.wsi_auth_required:
        return await call_next(request)

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = validate_wsi_token(
            authorization[7:].strip(),
            settings.wsi_auth_secret,
            settings.wsi_auth_audience,
            settings.wsi_auth_max_ttl,
        )
    except InvalidWsiToken:
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    request.state.wsi_token_payload = claims
    request.state.wsi_claims = claims
    return await call_next(request)


@app.middleware("http")
async def wsi_namespace(request, call_next):
    """Expose the API under /wsi without changing its internal route paths."""
    path = request.scope["path"]
    if path == "/wsi" or path.startswith("/wsi/"):
        request.scope["path"] = path[4:] or "/"
    return await call_next(request)

TILE_CACHE_HEADERS  = {"Cache-Control": "private, max-age=3600", "Vary": "Authorization"}
THUMB_CACHE_HEADERS = {"Cache-Control": "private, max-age=300", "Vary": "Authorization"}
# Metadata and search responses contain patient/slide information.
PHI_CACHE_HEADERS   = {"Cache-Control": "private, no-store", "Vary": "Authorization"}


async def _in_thread(fn, *args):
    """Run a blocking function in the default thread-pool executor."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def _run_image_operation(fn, *args):
    if _image_operation_semaphore is None:
        return await _in_thread(fn, *args)
    async with _image_operation_semaphore:
        async with track_image_operation():
            return await _in_thread(fn, *args)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_slide_id(image_id: str, study_id: str | None = None) -> str:
    """Resolve an image_id to a local path or S3 URI, with in-process caching."""
    cache_key = f"{study_id}:{image_id}" if study_id else image_id
    if cache_key in _path_cache:
        _path_cache.move_to_end(cache_key)
        return _path_cache[cache_key]
    test_path = settings.test_slide_map.get(image_id)
    if test_path:
        _path_cache[cache_key] = test_path
        _path_cache.move_to_end(cache_key)
        while len(_path_cache) > settings.path_cache_capacity:
            _path_cache.popitem(last=False)
        return test_path
    if study_id:
        binding = get_resource_index(settings.wsi_resource_index_file).slide_binding(study_id, image_id)
        if not binding or not binding.get("source_path"):
            raise ResourceIndexUnavailable(
                "study-qualified slide binding is missing a source path"
            )
        path = str(binding["source_path"])
        _path_cache[cache_key] = path
        _path_cache.move_to_end(cache_key)
        while len(_path_cache) > settings.path_cache_capacity:
            _path_cache.popitem(last=False)
        return path
    path = meta.get_slide_path(image_id, settings.databricks_warehouse_id)
    if not path:
        raise FileNotFoundError(f"Slide not found: {image_id}")
    _path_cache[cache_key] = path
    _path_cache.move_to_end(cache_key)
    while len(_path_cache) > settings.path_cache_capacity:
        _path_cache.popitem(last=False)
    return path


def _get_slide(image_id: str, study_id: str | None = None):
    """Resolve image_id → S3 path, open/retrieve from cache; raise 404 on failure."""
    try:
        s3_uri = _resolve_slide_id(image_id, study_id)
        return _slides.get(s3_uri)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Slide not found: {image_id}")
    except ResourceIndexUnavailable:
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    except Exception:
        logger.exception("Failed to open slide %s", image_id)
        raise HTTPException(status_code=500, detail="Failed to open slide")


def _authorize_resource(request: Request, resource_type: str, resource_id: str) -> str | None:
    """Bind a protected resource to the study in the validated capability."""
    if not settings.wsi_auth_required:
        return None

    claims = getattr(request.state, "wsi_claims", None)
    if not claims:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    study_id = claims["study_id"]
    requested_study = request.query_params.get("studyId")
    if requested_study is not None and requested_study != study_id:
        raise HTTPException(status_code=403, detail="study does not match capability")

    try:
        index = get_resource_index(settings.wsi_resource_index_file)
        allowed = index.contains(study_id, resource_type, str(resource_id))
    except ResourceIndexUnavailable:
        logger.error("WSI resource index is unavailable; refusing protected resource")
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    if not allowed:
        raise HTTPException(status_code=403, detail="resource is outside capability study")
    return study_id


def _authenticated_search_suggestions(request: Request, query: str) -> tuple[str, list[dict]]:
    claims = getattr(request.state, "wsi_claims", None)
    if not claims:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    try:
        study_id = claims["study_id"]
        return study_id, get_resource_index(settings.wsi_resource_index_file).suggestions(
            study_id, query
        )
    except ResourceIndexUnavailable:
        logger.error("WSI resource index is unavailable; refusing protected search")
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "n_workers": settings.n_workers}


@app.get("/metrics", include_in_schema=False)
def metrics():
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type, headers=PHI_CACHE_HEADERS)


@app.get("/slides/{image_id}/dbmeta")
async def slide_dbmeta(request: Request, image_id: str):
    """Return the raw Databricks metadata row for a single slide (by numeric image_id)."""
    study_id = _authorize_resource(request, "slides", image_id)
    binding = (
        get_resource_index(settings.wsi_resource_index_file).slide_binding(study_id, image_id)
        if study_id
        else None
    )
    if study_id and binding is None:
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    try:
        result = await _in_thread(
            get_slide_dbmeta,
            image_id,
            settings.databricks_warehouse_id,
            binding.get("patient_id") if binding else None,
        )
    except Exception:
        logger.exception("Databricks query failed for slide %s", image_id)
        raise HTTPException(status_code=502, detail="Metadata query failed")

    if result is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    return Response(content=json.dumps(result, default=str),
                    media_type="application/json", headers=PHI_CACHE_HEADERS)


@app.get("/search")
async def search(request: Request, q: str = ""):
    """
    Autocomplete suggestions for the search bar.

    Returns up to 8 items: [{ type, id, label, sublabel }]
    Detects query pattern: P-xxx → patients, P-xxx-Tx → samples, digits → slides.
    Results are cached in Redis for 5 minutes.
    """
    q = q.strip()
    if len(q) < 2:
        return Response(content="[]", media_type="application/json", headers=PHI_CACHE_HEADERS)

    if settings.wsi_auth_required:
        study_id, results = _authenticated_search_suggestions(request, q)
        cache_key = f"search:{study_id}:{q.lower()}"
        cached = await tile_cache.get_raw(cache_key)
        if cached is not None:
            return Response(
                content=json.dumps(cached, default=str),
                media_type="application/json",
                headers=PHI_CACHE_HEADERS,
            )
        await tile_cache.set_raw(cache_key, results, ttl=300)
        return Response(
            content=json.dumps(results, default=str),
            media_type="application/json",
            headers=PHI_CACHE_HEADERS,
        )

    cache_key = f"search:{q.lower()}"
    cached = await tile_cache.get_raw(cache_key)
    if cached is not None:
        return Response(content=json.dumps(cached, default=str),
                        media_type="application/json", headers=PHI_CACHE_HEADERS)

    try:
        results = await _in_thread(
            search_suggestions,
            q,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Search query failed for %r", q)
        raise HTTPException(status_code=502, detail="Search query failed")

    await tile_cache.set_raw(cache_key, results, ttl=300)
    return Response(content=json.dumps(results, default=str),
                    media_type="application/json", headers=PHI_CACHE_HEADERS)


@app.get("/tiles/{slide_id}/metadata")
async def metadata(request: Request, slide_id: str):
    study_id = _authorize_resource(request, "slides", slide_id)
    cache_key = f"{study_id}:{slide_id}" if study_id else slide_id
    cached = await tile_cache.get_metadata(cache_key)
    if cached is not None:
        return Response(content=json.dumps(cached), media_type="application/json",
                        headers=PHI_CACHE_HEADERS)
    slide = await _run_image_operation(_get_slide, slide_id, study_id)
    result = await _run_image_operation(slide_metadata, slide)
    await tile_cache.set_metadata(cache_key, result)
    return Response(content=json.dumps(result), media_type="application/json",
                    headers=PHI_CACHE_HEADERS)


@app.get("/tiles/{slide_id}/warmup", include_in_schema=False)
async def warmup(request: Request, slide_id: str):
    """Fetch and discard the overview tile to prime the TiffSlide cache on this worker."""
    study_id = _authorize_resource(request, "slides", slide_id)
    try:
        slide = await _run_image_operation(_get_slide, slide_id, study_id)
        image, _ = await _run_image_operation(render_tile_image, slide, 0, 0, 0)
        image.close()
    except OverviewTooLarge as exc:
        logger.warning(
            "Warmup overview rejected for slide %s level=%d read=%dx%d pixels=%d limit=%d",
            slide_id,
            exc.best_level,
            exc.read_width,
            exc.read_height,
            exc.requested_pixels,
            exc.max_decode_pixels,
        )
    except Exception:
        pass
    return Response(
        content=json.dumps({"status": "ok"}),
        media_type="application/json",
        headers=PHI_CACHE_HEADERS,
    )


@app.get("/tiles/{slide_id}/thumbnail")
async def thumbnail(
    request: Request,
    slide_id: str,
    width: int = 256,
    height: int = 256,
):
    study_id = _authorize_resource(request, "slides", slide_id)
    cache_slide_id = f"{study_id}:{slide_id}" if study_id else slide_id
    width = max(1, min(width, 2048))
    height = max(1, min(height, 2048))

    cached = await tile_cache.get_thumbnail(cache_slide_id, width, height)
    if cached:
        return Response(content=cached, media_type="image/jpeg",
                        headers=THUMB_CACHE_HEADERS)

    cache_key = tile_cache.thumbnail_cache_key(cache_slide_id, width, height)

    async def _build_thumbnail():
        slide = await _run_image_operation(_get_slide, slide_id, study_id)
        image_bytes = await _run_image_operation(get_thumbnail_bytes, slide, width, height)
        await tile_cache.set_thumbnail(cache_slide_id, width, height, image_bytes)
        return image_bytes

    try:
        data = await _singleflight.do(cache_key, "thumbnail", _build_thumbnail)
    except OverviewTooLarge as exc:
        logger.warning(
            "Thumbnail overview rejected for slide %s level=%d read=%dx%d pixels=%d limit=%d",
            slide_id,
            exc.best_level,
            exc.read_width,
            exc.read_height,
            exc.requested_pixels,
            exc.max_decode_pixels,
        )
        DECODE_SOURCE_PIXELS.observe(exc.requested_pixels)
        OVERSIZED_DECODE_REJECTIONS.labels(kind="thumbnail").inc()
        raise HTTPException(status_code=422, detail={"error": "overview_requires_preprocessing"})
    return Response(content=data, media_type="image/jpeg",
                    headers=THUMB_CACHE_HEADERS)


@app.get("/tiles/{slide_id}/zxy/{z}/{x}/{y}")
async def tile(request: Request, slide_id: str, z: int, x: int, y: int):
    study_id = _authorize_resource(request, "slides", slide_id)
    cache_slide_id = f"{study_id}:{slide_id}" if study_id else slide_id
    cached = await tile_cache.get_tile(cache_slide_id, z, x, y)
    if cached:
        return Response(content=cached, media_type="image/jpeg",
                        headers=TILE_CACHE_HEADERS)

    try:
        cache_key = tile_cache.tile_cache_key(cache_slide_id, z, x, y)

        async def _build_tile():
            slide = await _run_image_operation(_get_slide, slide_id, study_id)
            image_bytes = await _run_image_operation(get_tile_bytes, slide, z, x, y)
            await tile_cache.set_tile(cache_slide_id, z, x, y, image_bytes)
            return image_bytes

        data = await _singleflight.do(cache_key, "tile", _build_tile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except OverviewTooLarge as exc:
        logger.warning(
            "Tile overview rejected for slide %s z=%d x=%d y=%d level=%d read=%dx%d pixels=%d limit=%d",
            slide_id,
            z,
            x,
            y,
            exc.best_level,
            exc.read_width,
            exc.read_height,
            exc.requested_pixels,
            exc.max_decode_pixels,
        )
        DECODE_SOURCE_PIXELS.observe(exc.requested_pixels)
        OVERSIZED_DECODE_REJECTIONS.labels(kind="tile").inc()
        raise HTTPException(status_code=422, detail={"error": "overview_requires_preprocessing"})
    except Exception:
        logger.exception("Tile extraction failed for %s z=%d x=%d y=%d",
                         slide_id, z, x, y)
        raise HTTPException(status_code=500, detail="Tile extraction failed")
    return Response(content=data, media_type="image/jpeg",
                    headers=TILE_CACHE_HEADERS)
