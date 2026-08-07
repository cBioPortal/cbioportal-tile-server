"""
Tile server — FastAPI application.

Endpoints:
  GET /health
  GET /ready
  GET /tiles/{slide_id}/metadata
  GET /thumbnails/{slide_id}?width=256&height=256
  GET /tiles/{slide_id}/zxy/{z}/{x}/{y}

All tile and thumbnail responses carry long-lived Cache-Control headers so a
CDN or nginx proxy_cache can absorb the bulk of repeat requests.
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path

# Ensure app.* loggers emit to stderr alongside uvicorn's own loggers.
# uvicorn's dictConfig only configures uvicorn.* — root logger has no handler
# by default, so INFO from app.* would be silently dropped without this.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from . import cache as tile_cache
from . import meta
from .auth import InvalidWsiToken, validate_wsi_token
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
from .meta import get_slide_dbmeta, search_suggestions
from .rate_limit import EXPENSIVE_PATH_PREFIXES, rate_limiter
from .resource_index import ResourceIndexUnavailable, get_resource_index
from .slides import SlideCache
from .thumbnail_store import (
    ThumbnailRecord,
    get_persisted_generated_thumbnail_record,
    get_thumbnail_record,
    render_thumbnail_response,
)
from .tiles import OverviewTooLarge, get_tile_bytes, render_tile_image, slide_metadata
from .tiles import get_placeholder_thumbnail_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_slides: SlideCache | None = None
# In-process cache: image_id → s3 URI (populated on first open, survives across requests)
_path_cache: OrderedDict[str, str] = OrderedDict()
_path_cache_lock = threading.Lock()
_image_operation_semaphore: asyncio.Semaphore | None = None
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


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
    if settings.wsi_auth_required and settings.wsi_resource_index_file:
        try:
            await _in_thread(
                get_resource_index(settings.wsi_resource_index_file).revision
            )
        except ResourceIndexUnavailable:
            logger.error("WSI resource index is unavailable at startup; protected requests will fail closed")
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
        logger.warning(
            "AWS_ENDPOINT_URL is not set — S3 requests will go to public AWS "
            "(set this to your Dell ECS endpoint in production)"
        )
    if not settings.thumbnail_manifest_uri.strip():
        logger.warning(
            "THUMBNAIL_MANIFEST_URI is not set; missing thumbnail artifacts cannot be generated on demand"
        )
    logger.info(
        "Tile server ready. max_open_slides=%d endpoint=%s",
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
    expose_headers=["X-Thumbnail-Status", "X-Thumbnail-Reason", "Retry-After"],
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
    if request.scope["path"] in ("/health", "/ready"):
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


async def _run_image_operation(fn, *args, operation_kind: str = "image"):
    if _image_operation_semaphore is None:
        started = time.perf_counter()
        try:
            return await _in_thread(fn, *args)
        finally:
            IMAGE_OPERATION_SECONDS.labels(kind=operation_kind).observe(time.perf_counter() - started)
    async with _image_operation_semaphore:
        async with track_image_operation():
            started = time.perf_counter()
            try:
                return await _in_thread(fn, *args)
            finally:
                IMAGE_OPERATION_SECONDS.labels(kind=operation_kind).observe(time.perf_counter() - started)


def _thumbnail_status_headers(status: str, reason: str) -> dict[str, str]:
    return {
        "X-Thumbnail-Status": status,
        "X-Thumbnail-Reason": reason,
    }


def _thumbnail_placeholder_ttl(reason: str) -> int | None:
    if reason not in {"missing", "decode_timeout", "unavailable"}:
        return None
    configured = max(1, settings.thumbnail_placeholder_cache_ttl)
    if settings.thumbnail_cache_ttl:
        return min(settings.thumbnail_cache_ttl, configured)
    return configured


def _thumbnail_response_headers(status: str, reason: str) -> dict[str, str]:
    headers = dict(THUMB_CACHE_HEADERS)
    if status == "placeholder":
        ttl = _thumbnail_placeholder_ttl(reason) or 1
        headers["Cache-Control"] = f"private, max-age={ttl}"
    headers.update(_thumbnail_status_headers(status, reason))
    return headers


def _log_thumbnail_outcome(
    *,
    width: int,
    height: int,
    elapsed_ms: float,
    outcome: str,
    reason: str,
) -> None:
    logger.info(
        "thumbnail_request width=%d height=%d elapsed_ms=%.1f outcome=%s reason=%s",
        width,
        height,
        elapsed_ms,
        outcome,
        reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_slide_id(image_id: str, study_id: str | None = None) -> str:
    """Resolve an image_id to a local path or S3 URI, with in-process caching."""
    cache_key = f"{study_id}:{image_id}" if study_id else image_id
    with _path_cache_lock:
        if cache_key in _path_cache:
            _path_cache.move_to_end(cache_key)
            return _path_cache[cache_key]

    def cache_path(path: str) -> str:
        with _path_cache_lock:
            _path_cache[cache_key] = path
            _path_cache.move_to_end(cache_key)
            while len(_path_cache) > settings.path_cache_capacity:
                _path_cache.popitem(last=False)
        return path

    test_path = settings.test_slide_map.get(image_id)
    if test_path:
        return cache_path(test_path)
    if study_id:
        binding = get_resource_index(settings.wsi_resource_index_file).slide_binding(study_id, image_id)
        if binding is None:
            raise ResourceIndexUnavailable(
                "study-qualified slide binding is missing"
            )
        if not binding.get("source_path"):
            raise FileNotFoundError(f"Slide not found: {image_id}")
        path = str(binding["source_path"])
        return cache_path(path)
    path = meta.get_slide_path(image_id, settings.databricks_warehouse_id)
    if not path:
        raise FileNotFoundError(f"Slide not found: {image_id}")
    return cache_path(path)


def _run_slide_operation(image_id: str, study_id: str | None, operation, *args):
    """Resolve and run one blocking image operation under a slide lease."""
    try:
        s3_uri = _resolve_slide_id(image_id, study_id)
        if isinstance(_slides, SlideCache):
            return _slides.run(s3_uri, operation, *args)
        # Test doubles and embedding callers may provide the legacy cache
        # interface. Production always uses SlideCache.run above.
        slide = _slides.get(s3_uri)
        return operation(slide, *args)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Slide not found: {image_id}")
    except ResourceIndexUnavailable:
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    except (HTTPException, OverviewTooLarge, ValueError):
        raise
    except Exception as exc:
        logger.error("Slide operation failed; error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Slide operation failed")


async def _run_slide_image_operation(
    image_id: str,
    study_id: str | None,
    operation,
    *args,
    operation_kind: str = "image",
):
    return await _run_image_operation(
        _run_slide_operation,
        image_id,
        study_id,
        operation,
        *args,
        operation_kind=operation_kind,
    )


async def _generate_thumbnail_record_on_demand(
    image_id: str,
    study_id: str | None,
):
    if not settings.thumbnail_manifest_uri.strip():
        return None
    try:
        source_uri = await _in_thread(_resolve_slide_id, image_id, study_id)
    except FileNotFoundError:
        return None

    if _image_operation_semaphore is None:
        return await _run_thumbnail_worker(image_id, source_uri)
    async with _image_operation_semaphore:
        async with track_image_operation():
            return await _run_thumbnail_worker(image_id, source_uri)


async def _stop_thumbnail_worker(process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.communicate()


async def _run_thumbnail_worker(image_id: str, source_uri: str):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.thumbnail_worker",
        "--image-id",
        image_id,
        "--source-uri",
        source_uri,
        "--master-size",
        str(settings.thumbnail_master_size),
        cwd=str(_REPOSITORY_ROOT),
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.thumbnail_timeout_sec,
        )
    except (asyncio.CancelledError, asyncio.TimeoutError):
        await _stop_thumbnail_worker(process)
        raise

    if process.returncode != 0:
        error = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"thumbnail worker failed ({process.returncode}): {error}")
    try:
        payload = json.loads(stdout)
        return ThumbnailRecord(
            image_id=str(payload["image_id"]),
            uri=str(payload["uri"]),
            width=max(1, int(payload["width"])),
            height=max(1, int(payload["height"])),
            content_type=str(payload.get("content_type") or "image/jpeg"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("thumbnail worker returned invalid metadata") from exc


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


def _authenticated_search_context(request: Request):
    claims = getattr(request.state, "wsi_claims", None)
    if not claims:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    try:
        study_id = claims["study_id"]
        index = get_resource_index(settings.wsi_resource_index_file)
        return study_id, index, index.revision()
    except ResourceIndexUnavailable:
        logger.error("WSI resource index is unavailable; refusing protected search")
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")


def _readiness_status() -> tuple[int, dict]:
    payload = {
        "status": "ok",
        "auth_required": settings.wsi_auth_required,
        "n_workers": settings.n_workers,
    }
    if not settings.wsi_auth_required:
        return 200, payload
    try:
        payload["resource_index_revision"] = get_resource_index(
            settings.wsi_resource_index_file
        ).revision()
        return 200, payload
    except ResourceIndexUnavailable as exc:
        payload["status"] = "unavailable"
        payload["reason"] = f"trusted WSI resource index is unavailable: {exc}"
        return 503, payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    thumbnail_generation = (
        {"status": "ready"}
        if settings.thumbnail_manifest_uri.strip()
        else {
            "status": "degraded",
            "reason": "thumbnail_manifest_uri_missing",
        }
    )
    return {
        "status": "ok",
        "n_workers": settings.n_workers,
        "thumbnail_generation": thumbnail_generation,
    }


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


@app.get("/slides/{image_id}/dbmeta")
async def slide_dbmeta(request: Request, image_id: str):
    """Return restricted diagnostic metadata for a single slide."""
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
    except Exception as exc:
        logger.error("Databricks query failed; error_type=%s", type(exc).__name__)
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
        study_id, index, revision = _authenticated_search_context(request)
        cache_key = f"search:{study_id}:{revision}:{q.lower()}"
        cached = await tile_cache.get_raw(cache_key)
        if cached is not None:
            return Response(
                content=json.dumps(cached, default=str),
                media_type="application/json",
                headers=PHI_CACHE_HEADERS,
            )
        results = await _in_thread(index.suggestions, study_id, q)
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
    except Exception as exc:
        logger.error("Search query failed; error_type=%s", type(exc).__name__)
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
    result = await _run_slide_image_operation(
        slide_id, study_id, slide_metadata, operation_kind="metadata"
    )
    await tile_cache.set_metadata(cache_key, result)
    return Response(content=json.dumps(result), media_type="application/json",
                    headers=PHI_CACHE_HEADERS)


@app.get("/tiles/{slide_id}/warmup", include_in_schema=False)
async def warmup(request: Request, slide_id: str):
    """Fetch and discard the overview tile to prime the TiffSlide cache on this worker."""
    study_id = _authorize_resource(request, "slides", slide_id)
    try:
        def _warmup(slide):
            image, _ = render_tile_image(slide, 0, 0, 0)
            return image

        image = await _run_slide_image_operation(
            slide_id, study_id, _warmup, operation_kind="warmup"
        )
        image.close()
    except OverviewTooLarge as exc:
        logger.warning(
            "Warmup overview rejected level=%d read=%dx%d pixels=%d limit=%d",
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


@app.get("/thumbnails/{slide_id}")
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
        status = await tile_cache.get_thumbnail_status(cache_slide_id, width, height)
        cached_status = str(status.get("status") or "ok") if status else "ok"
        cached_reason = str(status.get("reason") or "served") if status else "served"
        return Response(
            content=cached,
            media_type="image/jpeg",
            headers=_thumbnail_response_headers(cached_status, cached_reason),
        )

    cache_key = tile_cache.thumbnail_cache_key(cache_slide_id, width, height)
    started = time.perf_counter()

    async def _build_thumbnail():
        record = await _in_thread(get_thumbnail_record, slide_id)
        if record is None:
            record = await _in_thread(get_persisted_generated_thumbnail_record, slide_id)
        if record is None:
            record = await _generate_thumbnail_record_on_demand(slide_id, study_id)
        if record is None:
            data = get_placeholder_thumbnail_bytes(width, height)
            status = {"status": "placeholder", "reason": "missing"}
        else:
            try:
                data, status = await _in_thread(render_thumbnail_response, record, width, height)
            except FileNotFoundError:
                record = await _generate_thumbnail_record_on_demand(slide_id, study_id)
                if record is None:
                    raise
                data, status = await _in_thread(render_thumbnail_response, record, width, height)
        ttl = _thumbnail_placeholder_ttl(str(status["reason"])) if status["status"] == "placeholder" else None
        await tile_cache.set_thumbnail(cache_slide_id, width, height, data, ttl=ttl)
        await tile_cache.set_thumbnail_status(cache_slide_id, width, height, status, ttl=ttl)
        return data, status

    try:
        data, status = await _singleflight.do(cache_key, "thumbnail", _build_thumbnail)
    except asyncio.TimeoutError:
        data = get_placeholder_thumbnail_bytes(width, height)
        status = {"status": "placeholder", "reason": "decode_timeout"}
        ttl = _thumbnail_placeholder_ttl(str(status["reason"]))
        await tile_cache.set_thumbnail(cache_slide_id, width, height, data, ttl=ttl)
        await tile_cache.set_thumbnail_status(cache_slide_id, width, height, status, ttl=ttl)
        elapsed_ms = (time.perf_counter() - started) * 1000
        _log_thumbnail_outcome(
            width=width,
            height=height,
            elapsed_ms=elapsed_ms,
            outcome=str(status["status"]),
            reason=str(status["reason"]),
        )
        return Response(
            content=data,
            media_type="image/jpeg",
            headers=_thumbnail_response_headers(str(status["status"]), str(status["reason"])),
        )
    except Exception as exc:
        logger.error("thumbnail_request_failed error_type=%s", type(exc).__name__)
        data = get_placeholder_thumbnail_bytes(width, height)
        status = {"status": "placeholder", "reason": "unavailable"}
        ttl = _thumbnail_placeholder_ttl(str(status["reason"]))
        await tile_cache.set_thumbnail(cache_slide_id, width, height, data, ttl=ttl)
        await tile_cache.set_thumbnail_status(cache_slide_id, width, height, status, ttl=ttl)
        elapsed_ms = (time.perf_counter() - started) * 1000
        _log_thumbnail_outcome(
            width=width,
            height=height,
            elapsed_ms=elapsed_ms,
            outcome=str(status["status"]),
            reason=str(status["reason"]),
        )
        return Response(
            content=data,
            media_type="image/jpeg",
            headers=_thumbnail_response_headers(str(status["status"]), str(status["reason"])),
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    _log_thumbnail_outcome(
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
        outcome=str(status["status"]),
        reason=str(status["reason"]),
    )
    return Response(
        content=data,
        media_type="image/jpeg",
        headers=_thumbnail_response_headers(str(status["status"]), str(status["reason"])),
    )


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
            image_bytes = await _run_slide_image_operation(
                slide_id,
                study_id,
                get_tile_bytes,
                z,
                x,
                y,
                operation_kind="tile",
            )
            await tile_cache.set_tile(cache_slide_id, z, x, y, image_bytes)
            return image_bytes

        data = await _singleflight.do(cache_key, "tile", _build_tile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except OverviewTooLarge as exc:
        logger.warning(
            "Tile overview rejected z=%d x=%d y=%d level=%d read=%dx%d pixels=%d limit=%d",
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
    except Exception as exc:
        logger.error(
            "Tile extraction failed z=%d x=%d y=%d error_type=%s",
            z,
            x,
            y,
            type(exc).__name__,
        )
        raise HTTPException(status_code=500, detail="Tile extraction failed")
    return Response(content=data, media_type="image/jpeg",
                    headers=TILE_CACHE_HEADERS)
