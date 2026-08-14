"""URL-bound cBioPortal WSI pixel service.

The service deliberately has no clinical metadata, hierarchy, search, or
image-id lookup. cBioPortal supplies an exact source URL and a short-lived
slide capability; this process validates the capability and serves pixels.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from . import cache as tile_cache
from .auth import (
    InvalidWsiToken,
    source_digest,
    validate_wsi_auth_configuration,
    validate_wsi_token,
)
from .blockcache import get_blockcache_manager
from .config import settings
from .metrics import (
    CACHE_MISS_LEADERS,
    COALESCED_CACHE_MISS_REQUESTS,
    DECODE_SOURCE_PIXELS,
    IMAGE_OPERATION_SECONDS,
    OVERSIZED_DECODE_REJECTIONS,
    metrics_payload,
    track_image_operation,
)
from .rate_limit import rate_limiter
from .slides import SlideCache
from .thumbnail_store import ThumbnailRecord, render_thumbnail_response
from .tiles import OverviewTooLarge, get_tile_bytes

logger = logging.getLogger(__name__)

_slides: SlideCache | None = None
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
    blockcache_manager = get_blockcache_manager()
    blockcache_task = None
    if blockcache_manager.enabled:
        await _in_thread(blockcache_manager.prune)

        async def _prune_loop():
            while True:
                await asyncio.sleep(blockcache_manager.prune_interval_seconds)
                await _in_thread(blockcache_manager.prune)

        blockcache_task = asyncio.create_task(_prune_loop())
    if not settings.aws_endpoint_url:
        logger.warning("AWS_ENDPOINT_URL is not set; S3 requests use the default endpoint")
    logger.info(
        "URL-bound tile server ready. max_open_slides=%d endpoint=%s",
        settings.max_open_slides,
        settings.aws_endpoint_url or "AWS default",
    )
    try:
        yield
    finally:
        if blockcache_task is not None:
            blockcache_task.cancel()
            with suppress(asyncio.CancelledError):
                await blockcache_task
        _slides.close_all()
        await tile_cache.close_cache()


app = FastAPI(
    title="WSI Tile Server",
    description="Serve URL-bound whole-slide image tiles and thumbnails.",
    version="2.0.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def require_wsi_capability(request: Request, call_next):
    if request.scope["path"] in ("/health", "/ready"):
        return await call_next(request)
    # Browser clients send an unauthenticated OPTIONS request before any
    # cross-origin request that includes the Authorization header.  CORS
    # normally handles this outside the application stack, but keep the
    # capability guard safe if middleware ordering changes.
    if request.method == "OPTIONS":
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
    if claims.get("wsi_auth_version") != 2:
        return Response(status_code=403, content="slide-scoped capability is required")
    request.state.wsi_claims = claims
    if not rate_limiter.allow(claims["sub"], settings.rate_limit_per_minute):
        return Response(status_code=429, headers={"Retry-After": "60"}, content="Rate limit exceeded")
    return await call_next(request)


@app.middleware("http")
async def wsi_namespace(request, call_next):
    path = request.scope["path"]
    if path == "/wsi" or path.startswith("/wsi/"):
        request.scope["path"] = path[4:] or "/"
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
    expose_headers=["X-Thumbnail-Status", "X-Thumbnail-Reason", "Retry-After"],
)


TILE_CACHE_HEADERS = {"Cache-Control": "private, max-age=3600", "Vary": "Authorization"}
THUMB_CACHE_HEADERS = {"Cache-Control": "private, max-age=300", "Vary": "Authorization"}
PHI_CACHE_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Authorization"}


async def _in_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def _run_image_operation(fn, *args, operation_kind: str = "image"):
    started = time.perf_counter()
    if _image_operation_semaphore is None:
        try:
            return await _in_thread(fn, *args)
        finally:
            IMAGE_OPERATION_SECONDS.labels(kind=operation_kind).observe(time.perf_counter() - started)
    async with _image_operation_semaphore:
        async with track_image_operation():
            try:
                return await _in_thread(fn, *args)
            finally:
                IMAGE_OPERATION_SECONDS.labels(kind=operation_kind).observe(time.perf_counter() - started)


def _allowed_source(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise HTTPException(status_code=400, detail="source URL is required")
    parsed = urlparse(source)
    allowed = {item.strip().lower() for item in settings.wsi_allowed_source_schemes if item.strip()}
    if parsed.scheme.lower() not in allowed:
        raise HTTPException(status_code=400, detail="unsupported source URL")
    if parsed.scheme.lower() == "s3" and (not parsed.netloc or not parsed.path.strip("/")):
        raise HTTPException(status_code=400, detail="malformed source URL")
    if parsed.scheme.lower() == "file" and not parsed.path.startswith("/"):
        raise HTTPException(status_code=400, detail="malformed source URL")
    return source


def _claims(request: Request) -> dict:
    claims = getattr(request.state, "wsi_claims", None)
    if not claims:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    return claims


def _authorize_source(request: Request, source: str, operation: str) -> tuple[str, dict]:
    source = _allowed_source(source)
    claims = _claims(request)
    if claims.get("wsi_auth_version") != 2:
        raise HTTPException(status_code=403, detail="slide-scoped capability is required")
    claim_name = "tile_source_sha256" if operation == "tile" else "thumbnail_source_sha256"
    if not hmac.compare_digest(source_digest(source), claims[claim_name]):
        raise HTTPException(status_code=403, detail="source is outside capability")
    return source, claims


def _run_slide_operation(source: str, operation, *args):
    try:
        if not isinstance(_slides, SlideCache):
            raise RuntimeError("slide cache is not initialized")
        return _slides.run(source, operation, *args)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="source slide not found")
    except (HTTPException, OverviewTooLarge, ValueError):
        raise
    except Exception as exc:
        logger.error("Slide operation failed; error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Slide operation failed")


async def _run_slide_image_operation(source: str, operation, *args, operation_kind: str = "image"):
    return await _run_image_operation(
        _run_slide_operation, source, operation, *args, operation_kind=operation_kind
    )


def _read_thumbnail(source: str, width: int, height: int, master_width: int, master_height: int):
    record = ThumbnailRecord(
        image_id=source_digest(source),
        uri=source,
        width=master_width,
        height=master_height,
        content_type="image/jpeg",
    )
    return render_thumbnail_response(record, width, height)


def _readiness_status() -> tuple[int, dict]:
    payload = {
        "status": "ok",
        "auth_required": True,
        "auth_contract_version": 2,
        "n_workers": settings.n_workers,
    }
    try:
        validate_wsi_auth_configuration(
            settings.wsi_auth_secret,
            settings.wsi_auth_audience,
            settings.wsi_auth_max_ttl,
        )
        return 200, payload
    except InvalidWsiToken:
        payload["status"] = "unavailable"
        payload["reason"] = "WSI authentication is not configured"
        return 503, payload


@app.get("/health")
def health():
    return {"status": "ok", "n_workers": settings.n_workers, "auth_contract_version": 2}


@app.get("/ready")
def ready():
    status_code, payload = _readiness_status()
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        headers=PHI_CACHE_HEADERS,
        status_code=status_code,
    )


@app.get("/metrics", include_in_schema=False)
def metrics():
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type, headers=PHI_CACHE_HEADERS)


@app.get("/tiles/zxy/{z}/{x}/{y}")
async def tile(
    request: Request,
    z: int,
    x: int,
    y: int,
    source: str = Query(...),
):
    source, _ = _authorize_source(request, source, "tile")
    cache_key = source_digest(source)
    cached = await tile_cache.get_tile(cache_key, z, x, y)
    if cached:
        return Response(content=cached, media_type="image/jpeg", headers=TILE_CACHE_HEADERS)

    async def _build_tile():
        image_bytes = await _run_slide_image_operation(
            source, get_tile_bytes, z, x, y, operation_kind="tile"
        )
        await tile_cache.set_tile(cache_key, z, x, y, image_bytes)
        return image_bytes

    try:
        data = await _singleflight.do(tile_cache.tile_cache_key(cache_key, z, x, y), "tile", _build_tile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except OverviewTooLarge as exc:
        logger.warning(
            "Tile overview rejected z=%d x=%d y=%d level=%d read=%dx%d pixels=%d limit=%d",
            exc.z, exc.x, exc.y, exc.best_level, exc.read_width, exc.read_height,
            exc.requested_pixels, exc.max_decode_pixels,
        )
        DECODE_SOURCE_PIXELS.observe(exc.requested_pixels)
        OVERSIZED_DECODE_REJECTIONS.labels(kind="tile").inc()
        raise HTTPException(status_code=422, detail={"error": "overview_requires_preprocessing"})
    return Response(content=data, media_type="image/jpeg", headers=TILE_CACHE_HEADERS)


@app.get("/thumbnails")
async def thumbnail(
    request: Request,
    source: str = Query(...),
    width: int = 256,
    height: int = 256,
):
    source, claims = _authorize_source(request, source, "thumbnail")
    width = max(1, min(width, 2048))
    height = max(1, min(height, 2048))
    cache_key = source_digest(source)
    cached = await tile_cache.get_thumbnail(cache_key, width, height)
    if cached:
        return Response(content=cached, media_type="image/jpeg", headers=THUMB_CACHE_HEADERS)

    async def _build_thumbnail():
        data, status = await _in_thread(
            _read_thumbnail,
            source,
            width,
            height,
            claims["thumbnail_width"],
            claims["thumbnail_height"],
        )
        await tile_cache.set_thumbnail(cache_key, width, height, data)
        return data, status

    try:
        data, status = await _singleflight.do(
            tile_cache.thumbnail_cache_key(cache_key, width, height),
            "thumbnail",
            _build_thumbnail,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="thumbnail not found")
    except Exception as exc:
        logger.error("Thumbnail fetch failed; error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="thumbnail unavailable")
    headers = dict(THUMB_CACHE_HEADERS)
    headers.update({
        "X-Thumbnail-Status": str(status.get("status") or "ok"),
        "X-Thumbnail-Reason": str(status.get("reason") or "served"),
    })
    return Response(content=data, media_type="image/jpeg", headers=headers)
