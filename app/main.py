"""URL-bound cBioPortal WSI pixel service.

The service deliberately has no clinical metadata, hierarchy, search, or
image-id lookup. cBioPortal supplies an exact source URL and a short-lived
slide capability; this process validates the capability and serves pixels.
"""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import logging
import random
import time
import traceback
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlparse

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionError as BotoConnectionError,
    HTTPClientError,
)
from fsspec.exceptions import BlocksizeMismatchError
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from tifffile import TiffFileError

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
    IMAGE_OPERATION_QUEUE_SECONDS,
    IMAGE_OPERATION_QUEUE_TIMEOUTS,
    OVERSIZED_DECODE_REJECTIONS,
    SLIDE_CACHE_REPAIRS,
    SLIDE_OPERATION_ERRORS,
    THUMBNAIL_FETCH_ERRORS,
    THUMBNAIL_FETCH_QUEUE_SECONDS,
    THUMBNAIL_FETCH_RETRIES,
    THUMBNAIL_FETCH_SECONDS,
    THUMBNAIL_RESIZE_SECONDS,
    metrics_payload,
    track_image_operation,
    track_thumbnail_fetch,
)
from .slides import SlideCache
from .thumbnail_store import (
    ThumbnailRecord,
    close_runtime_store,
    initialize_runtime_store,
    read_thumbnail_bytes,
    render_thumbnail_payload,
)
from .tiles import OverviewTooLarge, get_tile_bytes

logger = logging.getLogger(__name__)

_slides: SlideCache | None = None
_image_operation_semaphore: asyncio.Semaphore | None = None
_thumbnail_fetch_semaphore: asyncio.Semaphore | None = None


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


class ImageOperationQueueTimeout(Exception):
    """Raised when an image request cannot start within its bounded queue wait."""


_singleflight = _SingleFlight()


class CacheMissRateLimitExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("cache-miss rate limit exceeded")
        self.retry_after = retry_after


class DistributedMissTimeout(Exception):
    pass


_TRANSIENT_THUMBNAIL_S3_ERROR_CODES = frozenset(
    {
        "InternalError",
        "InternalFailure",
        "OperationAborted",
        "RequestTimeout",
        "RequestTimeoutException",
        "ServiceUnavailable",
        "SlowDown",
        "Throttling",
        "ThrottlingException",
    }
)
_TRANSIENT_THUMBNAIL_ERRNOS = frozenset(
    errno_value
    for errno_value in (
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EBUSY", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "ECONNREFUSED", None),
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "EHOSTDOWN", None),
        getattr(errno, "EHOSTUNREACH", None),
        getattr(errno, "ENETDOWN", None),
        getattr(errno, "ENETRESET", None),
        getattr(errno, "ENETUNREACH", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ETIMEDOUT", None),
        getattr(errno, "EREMOTEIO", None),
        getattr(errno, "EREMCHG", None),
    )
    if errno_value is not None
)


def _is_retryable_thumbnail_fetch_error(exc: Exception) -> bool:
    """Return whether an object-store read can succeed on a later attempt."""
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return False
    if isinstance(
        exc,
        (BotoConnectionError, HTTPClientError, ConnectionError, TimeoutError),
    ):
        return True
    if isinstance(exc, ClientError):
        response = getattr(exc, "response", {}) or {}
        error = response.get("Error", {}) or {}
        code = str(error.get("Code", ""))
        if code in _TRANSIENT_THUMBNAIL_S3_ERROR_CODES:
            return True
        status = (response.get("ResponseMetadata", {}) or {}).get(
            "HTTPStatusCode"
        )
        return isinstance(status, int) and (status in (408, 429) or status >= 500)
    if isinstance(exc, BotoCoreError):
        return True
    if isinstance(exc, OSError):
        return exc.errno is None or exc.errno in _TRANSIENT_THUMBNAIL_ERRNOS
    return False


def _is_retryable_slide_source_error(exc: Exception) -> bool:
    """Return whether a slide source read may succeed on a later request."""
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return False
    if isinstance(exc, ClientError):
        response = getattr(exc, "response", {}) or {}
        error = response.get("Error", {}) or {}
        code = str(error.get("Code", ""))
        if code in _TRANSIENT_THUMBNAIL_S3_ERROR_CODES:
            return True
        status = (response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
        return isinstance(status, int) and (status in (408, 429) or status >= 500)
    if isinstance(exc, (BotoCoreError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno is None or exc.errno in _TRANSIENT_THUMBNAIL_ERRNOS
    return False


def _is_cache_repairable_slide_error(exc: Exception) -> bool:
    """Identify failures for which reopening after purging the local cache helps."""
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return False
    if isinstance(exc, OSError):
        if isinstance(exc, (BotoCoreError, ConnectionError, TimeoutError)):
            return False
        return exc.errno not in _TRANSIENT_THUMBNAIL_ERRNOS
    if isinstance(exc, (BlocksizeMismatchError, EOFError, TiffFileError)):
        return True
    return type(exc).__module__.startswith("imagecodecs")


def _traceback_frames(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    return " <- ".join(
        f"{frame.filename}:{frame.lineno}:{frame.name}" for frame in frames
    )


def _attempt_slide_cache_repair(source: str, exc: Exception) -> bool:
    if not isinstance(_slides, SlideCache):
        return False
    try:
        repaired = _slides.repair(source)
    except Exception as repair_exc:  # noqa: BLE001 - preserve the original failure
        logger.error(
            "Slide cache repair failed; source_digest=%s error_type=%s repair_error_type=%s",
            source_digest(source)[:16],
            type(exc).__name__,
            type(repair_exc).__name__,
        )
        return False
    if repaired:
        SLIDE_CACHE_REPAIRS.labels(
            outcome="attempted", error_type=type(exc).__name__
        ).inc()
        logger.warning(
            "Purged worker-local slide cache; source_digest=%s error_type=%s",
            source_digest(source)[:16],
            type(exc).__name__,
        )
    return repaired


async def _run_with_miss_lock_lease(cache_key: str, token: str, producer):
    """Keep a distributed miss lock alive while its owner is working."""
    stop = asyncio.Event()
    interval = max(1.0, min(30.0, settings.cache_miss_lock_ttl_seconds / 3))

    async def renew_until_done():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                if not await tile_cache.renew_miss_lock(cache_key, token):
                    return

    renewal_task = asyncio.create_task(renew_until_done())
    try:
        return await producer()
    finally:
        stop.set()
        renewal_task.cancel()
        with suppress(asyncio.CancelledError):
            await renewal_task


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _slides, _image_operation_semaphore, _thumbnail_fetch_semaphore
    _slides = SlideCache(capacity=settings.max_open_slides)
    _image_operation_semaphore = asyncio.Semaphore(settings.max_image_operations)
    _thumbnail_fetch_semaphore = asyncio.Semaphore(settings.thumbnail_fetch_concurrency)
    await tile_cache.init_cache()
    try:
        await _in_thread(initialize_runtime_store)
    except Exception as exc:
        logger.warning("Thumbnail S3 client prewarm failed; using lazy initialization: %s", type(exc).__name__)
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
        await _in_thread(close_runtime_store)
        await tile_cache.close_cache()


app = FastAPI(
    title="WSI Tile Server",
    description="Serve URL-bound whole-slide image tiles and thumbnails.",
    version="2.0.0",
    lifespan=lifespan,
)

@app.middleware("http")
async def require_wsi_capability(request: Request, call_next):
    if request.scope["path"] in ("/health", "/ready", "/metrics"):
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
    allow_private_network=True,
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
    queue_started = time.perf_counter()
    try:
        await asyncio.wait_for(
            _image_operation_semaphore.acquire(),
            timeout=max(0.1, settings.image_operation_queue_timeout_seconds),
        )
    except asyncio.TimeoutError as exc:
        IMAGE_OPERATION_QUEUE_TIMEOUTS.labels(kind=operation_kind).inc()
        raise ImageOperationQueueTimeout from exc
    IMAGE_OPERATION_QUEUE_SECONDS.labels(kind=operation_kind).observe(
        time.perf_counter() - queue_started
    )
    try:
        async with track_image_operation():
            return await _in_thread(fn, *args)
    finally:
        _image_operation_semaphore.release()
        IMAGE_OPERATION_SECONDS.labels(kind=operation_kind).observe(time.perf_counter() - started)


async def _distributed_singleflight(
    cache_key: str,
    kind: str,
    subject: str,
    scope: str,
    producer,
    read_cached,
    decode_cached,
):
    """Coalesce a cache miss across workers, with local fallback if Redis is down."""
    async def coordinated_producer():
        async def run_owner(lock: str):
            cached = await read_cached()
            if cached is not None:
                return decode_cached(cached)
            allowed, retry_after = await tile_cache.allow_cache_miss(subject, scope)
            if not allowed:
                raise CacheMissRateLimitExceeded(retry_after)
            return await producer()

        lock = await tile_cache.try_acquire_miss_lock(cache_key, kind)
        if lock is None:
            return await producer()
        if lock:
            try:
                return await _run_with_miss_lock_lease(
                    cache_key, lock, lambda: run_owner(lock)
                )
            finally:
                await tile_cache.release_miss_lock(cache_key, lock)

        deadline = time.monotonic() + max(0.1, settings.cache_miss_wait_timeout_seconds)
        while time.monotonic() < deadline:
            cached = await read_cached()
            if cached is not None:
                return decode_cached(cached)
            await asyncio.sleep(0.02 if time.monotonic() + 1 < deadline else 0.1)
            lock = await tile_cache.try_acquire_miss_lock(cache_key, kind)
            if lock is None:
                return await producer()
            if lock:
                try:
                    return await _run_with_miss_lock_lease(
                        cache_key, lock, lambda: run_owner(lock)
                    )
                finally:
                    await tile_cache.release_miss_lock(cache_key, lock)
        raise DistributedMissTimeout()

    return await _singleflight.do(cache_key, kind, coordinated_producer)


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

def _slide_rate_limit_scope(claims: dict) -> str:
    return f"{claims['study_id']}\0{claims['image_id']}"



def _authorize_source(request: Request, source: str, operation: str) -> tuple[str, dict]:
    source = _allowed_source(source)
    claims = _claims(request)
    if claims.get("wsi_auth_version") != 2:
        raise HTTPException(status_code=403, detail="slide-scoped capability is required")
    claim_name = "tile_source_sha256" if operation == "tile" else "thumbnail_source_sha256"
    if not hmac.compare_digest(source_digest(source), claims[claim_name]):
        raise HTTPException(status_code=403, detail="source is outside capability")
    return source, claims


def _source_from_request(request: Request, query_source: str | None) -> str:
    header_source = request.headers.get("x-wsi-source", "").strip()
    query_source = (query_source or "").strip()
    if header_source and query_source and not hmac.compare_digest(header_source, query_source):
        raise HTTPException(status_code=400, detail="conflicting source bindings")
    return header_source or query_source


def _run_slide_operation(source: str, operation, *args):
    repair_attempted = False
    while True:
        try:
            if not isinstance(_slides, SlideCache):
                raise RuntimeError("slide cache is not initialized")
            result = _slides.run(source, operation, *args)
            if repair_attempted:
                SLIDE_CACHE_REPAIRS.labels(
                    outcome="recovered", error_type="reopened"
                ).inc()
            return result
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="source slide not found")
        except (HTTPException, OverviewTooLarge):
            raise
        except BlocksizeMismatchError as exc:
            if not repair_attempted and _attempt_slide_cache_repair(source, exc):
                repair_attempted = True
                continue
            if repair_attempted:
                SLIDE_CACHE_REPAIRS.labels(
                    outcome="failed", error_type=type(exc).__name__
                ).inc()
            SLIDE_OPERATION_ERRORS.labels(error_type=type(exc).__name__).inc()
            logger.error(
                "Slide operation failed; source_digest=%s error_type=%s stack=%s",
                source_digest(source)[:16],
                type(exc).__name__,
                _traceback_frames(exc),
            )
            raise HTTPException(status_code=500, detail="Slide operation failed") from exc
        except ValueError:
            raise
        except Exception as exc:
            if (
                not repair_attempted
                and _is_cache_repairable_slide_error(exc)
                and _attempt_slide_cache_repair(source, exc)
            ):
                repair_attempted = True
                continue
            if repair_attempted:
                SLIDE_CACHE_REPAIRS.labels(
                    outcome="failed", error_type=type(exc).__name__
                ).inc()
            if _is_retryable_slide_source_error(exc):
                logger.warning(
                    "Slide source temporarily unavailable; source_digest=%s error_type=%s stack=%s",
                    source_digest(source)[:16],
                    type(exc).__name__,
                    _traceback_frames(exc),
                )
                raise HTTPException(
                    status_code=503,
                    headers={"Retry-After": "1"},
                    detail="Slide source temporarily unavailable",
                ) from exc
            SLIDE_OPERATION_ERRORS.labels(error_type=type(exc).__name__).inc()
            logger.error(
                "Slide operation failed; source_digest=%s error_type=%s stack=%s",
                source_digest(source)[:16],
                type(exc).__name__,
                _traceback_frames(exc),
            )
            raise HTTPException(status_code=500, detail="Slide operation failed") from exc


async def _run_slide_image_operation(source: str, operation, *args, operation_kind: str = "image"):
    return await _run_image_operation(
        _run_slide_operation, source, operation, *args, operation_kind=operation_kind
    )


async def _run_thumbnail_fetch(record: ThumbnailRecord) -> bytes:
    started = time.perf_counter()
    queue_started = time.perf_counter()
    semaphore = _thumbnail_fetch_semaphore

    async def read_with_retry() -> bytes:
        attempts = max(1, settings.thumbnail_fetch_max_attempts)
        retry_delay = max(0.0, settings.thumbnail_fetch_retry_delay_sec)
        retried = False
        for attempt in range(attempts):
            try:
                payload = await _in_thread(read_thumbnail_bytes, record)
                if retried:
                    THUMBNAIL_FETCH_RETRIES.labels(outcome="recovered").inc()
                return payload
            except Exception as exc:
                retryable = _is_retryable_thumbnail_fetch_error(exc)
                if (
                    attempt + 1 >= attempts
                    or not retryable
                ):
                    if retried and retryable:
                        THUMBNAIL_FETCH_RETRIES.labels(outcome="exhausted").inc()
                    raise
                retried = True
                if retry_delay:
                    delay = min(1.0, retry_delay * (2**attempt))
                    await asyncio.sleep(delay * random.uniform(0.8, 1.2))
        raise RuntimeError("thumbnail fetch retry loop did not return")

    if semaphore is None:
        try:
            return await read_with_retry()
        except Exception:
            THUMBNAIL_FETCH_ERRORS.inc()
            raise
        finally:
            THUMBNAIL_FETCH_SECONDS.observe(time.perf_counter() - started)
    async with semaphore:
        THUMBNAIL_FETCH_QUEUE_SECONDS.observe(time.perf_counter() - queue_started)
        try:
            async with track_thumbnail_fetch():
                return await read_with_retry()
        except Exception:
            THUMBNAIL_FETCH_ERRORS.inc()
            raise
        finally:
            THUMBNAIL_FETCH_SECONDS.observe(time.perf_counter() - started)


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
    source: str | None = Query(None),
):
    source = _source_from_request(request, source)
    source, claims = _authorize_source(request, source, "tile")
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
        tile_key = tile_cache.tile_cache_key(cache_key, z, x, y)
        data = await _distributed_singleflight(
            tile_key,
            "tile",
            claims["sub"],
            _slide_rate_limit_scope(claims),
            _build_tile,
            lambda: tile_cache.get_tile(cache_key, z, x, y),
            lambda cached: cached,
        )
    except CacheMissRateLimitExceeded as exc:
        raise HTTPException(status_code=429, headers={"Retry-After": str(exc.retry_after)}, detail="Rate limit exceeded")
    except DistributedMissTimeout:
        raise HTTPException(status_code=503, headers={"Retry-After": "1"}, detail="Tile extraction is still in progress")
    except ImageOperationQueueTimeout:
        raise HTTPException(status_code=503, headers={"Retry-After": "1"}, detail="Tile service is busy")
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
    source: str | None = Query(None),
    width: int = 256,
    height: int = 256,
):
    source = _source_from_request(request, source)
    source, claims = _authorize_source(request, source, "thumbnail")
    width = max(1, min(width, 2048))
    height = max(1, min(height, 2048))
    cache_key = source_digest(source)
    cached = await tile_cache.get_thumbnail(cache_key, width, height)
    if cached:
        return Response(content=cached, media_type="image/jpeg", headers=THUMB_CACHE_HEADERS)

    async def _build_thumbnail():
        record = ThumbnailRecord(
            image_id=source_digest(source),
            uri=source,
            width=claims["thumbnail_width"],
            height=claims["thumbnail_height"],
            content_type="image/jpeg",
        )
        payload = await _run_thumbnail_fetch(record)
        if width >= record.width and height >= record.height:
            data, status = payload, {"status": "ok", "reason": "master"}
        else:
            resize_started = time.perf_counter()
            try:
                data, status = await _run_image_operation(
                    render_thumbnail_payload,
                    payload,
                    record,
                    width,
                    height,
                    operation_kind="thumbnail_resize",
                )
            finally:
                THUMBNAIL_RESIZE_SECONDS.observe(time.perf_counter() - resize_started)
        await tile_cache.set_thumbnail(cache_key, width, height, data)
        return data, status

    try:
        thumbnail_key = tile_cache.thumbnail_cache_key(cache_key, width, height)
        data, status = await _distributed_singleflight(
            thumbnail_key,
            "thumbnail",
            claims["sub"],
            _slide_rate_limit_scope(claims),
            _build_thumbnail,
            lambda: tile_cache.get_thumbnail(cache_key, width, height),
            lambda cached: (cached, {"status": "ok", "reason": "distributed-cache"}),
        )
    except CacheMissRateLimitExceeded as exc:
        raise HTTPException(status_code=429, headers={"Retry-After": str(exc.retry_after)}, detail="Rate limit exceeded")
    except DistributedMissTimeout:
        raise HTTPException(status_code=503, headers={"Retry-After": "1"}, detail="Thumbnail extraction is still in progress")
    except ImageOperationQueueTimeout:
        raise HTTPException(status_code=503, headers={"Retry-After": "1"}, detail="Thumbnail service is busy")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="thumbnail not found")
    except Exception as exc:
        if _is_retryable_thumbnail_fetch_error(exc):
            logger.warning(
                "Thumbnail source temporarily unavailable; source_digest=%s error_type=%s stack=%s",
                source_digest(source)[:16],
                type(exc).__name__,
                _traceback_frames(exc),
            )
            raise HTTPException(
                status_code=503,
                headers={"Retry-After": "1"},
                detail="Thumbnail source temporarily unavailable",
            ) from exc
        logger.error(
            "Thumbnail fetch failed; source_digest=%s error_type=%s stack=%s",
            source_digest(source)[:16],
            type(exc).__name__,
            _traceback_frames(exc),
        )
        raise HTTPException(status_code=502, detail="thumbnail unavailable")
    headers = dict(THUMB_CACHE_HEADERS)
    headers.update({
        "X-Thumbnail-Status": str(status.get("status") or "ok"),
        "X-Thumbnail-Reason": str(status.get("reason") or "served"),
    })
    return Response(content=data, media_type="image/jpeg", headers=headers)
