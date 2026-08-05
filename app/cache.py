"""
Redis tile cache.

Keys:  tile:{slide_id}:{z}:{x}:{y}
Value: raw JPEG bytes

Tiles are immutable so tile TTL is configured separately. A thumbnail cache
uses the key thumbnail:{slide_id}:{width}:{height}.

Patient hierarchy is owned by the cBioPortal backend. This cache is limited to
slide tiles, thumbnails, slide metadata, and short-lived search results.
"""

import json
import logging
import time

import redis.asyncio as aioredis

from .config import settings
from .metrics import CACHE_REQUESTS, REDIS_ERRORS, REDIS_OPERATION_SECONDS

_redis: aioredis.Redis | None = None
_redis_disabled_until = 0.0
_redis_failure_logged = False
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Key helpers — single source of truth for all cache key formats
# ---------------------------------------------------------------------------

def _tile_key(slide_id: str, z: int, x: int, y: int) -> str:
    return f"tile:{slide_id}:{z}:{x}:{y}"

def _thumb_key(slide_id: str, width: int, height: int) -> str:
    return f"thumbnail:{slide_id}:{width}:{height}"

def _meta_key(slide_id: str) -> str:
    return f"meta:{slide_id}"


def tile_cache_key(slide_id: str, z: int, x: int, y: int) -> str:
    return _tile_key(slide_id, z, x, y)


def thumbnail_cache_key(slide_id: str, width: int, height: int) -> str:
    return _thumb_key(slide_id, width, height)


def _redis_configured() -> bool:
    return bool(settings.redis_url) and settings.redis_url.startswith(
        ("redis://", "rediss://", "unix://")
    )


async def init_cache() -> None:
    global _redis, _redis_disabled_until, _redis_failure_logged
    _redis_disabled_until = 0.0
    _redis_failure_logged = False
    if not _redis_configured():
        _redis = None
        return  # no cache configured — all get/set calls are no-ops
    _redis = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=False,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_command_timeout_seconds,
    )


async def close_cache() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
    _redis = None


# ---------------------------------------------------------------------------
# Internal I/O primitives — single guard + try/except for all get/set paths
# ---------------------------------------------------------------------------

def _redis_available() -> bool:
    return _redis is not None and time.monotonic() >= _redis_disabled_until


def _trip_redis_breaker(operation: str) -> None:
    global _redis_disabled_until, _redis_failure_logged
    now = time.monotonic()
    _redis_disabled_until = max(
        _redis_disabled_until,
        now + max(0.0, settings.redis_failure_backoff_seconds),
    )
    if not _redis_failure_logged:
        logger.warning("Redis cache %s failed; bypassing Redis for %.1fs", operation, settings.redis_failure_backoff_seconds)
        _redis_failure_logged = True


def _record_redis_recovery() -> None:
    global _redis_failure_logged
    if _redis_failure_logged and time.monotonic() >= _redis_disabled_until:
        logger.info("Redis cache recovered")
        _redis_failure_logged = False


async def _redis_get(key: str, operation: str = "get") -> bytes | None:
    """Guarded GET; returns None when cache is unavailable or on error."""
    if not _redis_available():
        return None
    started = time.perf_counter()
    try:
        result = await _redis.get(key)
        REDIS_OPERATION_SECONDS.labels(operation=operation).observe(time.perf_counter() - started)
        _record_redis_recovery()
        return result
    except Exception:
        REDIS_OPERATION_SECONDS.labels(operation=operation).observe(time.perf_counter() - started)
        REDIS_ERRORS.labels(operation=operation).inc()
        _trip_redis_breaker(operation)
        return None


async def _redis_set(key: str, data: bytes | str, ttl: int = 0) -> None:
    """Guarded SET/SETEX; silently swallows errors so cache is never fatal."""
    if not _redis_available():
        return
    started = time.perf_counter()
    try:
        if ttl:
            await _redis.setex(key, ttl, data)
        else:
            await _redis.set(key, data)
        REDIS_OPERATION_SECONDS.labels(operation="set").observe(time.perf_counter() - started)
        _record_redis_recovery()
    except Exception:
        REDIS_OPERATION_SECONDS.labels(operation="set").observe(time.perf_counter() - started)
        REDIS_ERRORS.labels(operation="set").inc()
        _trip_redis_breaker("set")


def _from_json(raw: bytes | None) -> object | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


async def _redis_get_json(key: str) -> object | None:
    return _from_json(await _redis_get(key))


async def _redis_set_json(key: str, data: object, ttl: int = 0) -> None:
    await _redis_set(key, json.dumps(data, default=str), ttl=ttl)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_tile(slide_id: str, z: int, x: int, y: int) -> bytes | None:
    value = await _redis_get(_tile_key(slide_id, z, x, y), "get_tile")
    CACHE_REQUESTS.labels(kind="tile", result="hit" if value is not None else "miss").inc()
    return value


async def set_tile(slide_id: str, z: int, x: int, y: int, data: bytes) -> None:
    await _redis_set(_tile_key(slide_id, z, x, y), data, ttl=settings.tile_cache_ttl)


async def get_thumbnail(slide_id: str, width: int, height: int) -> bytes | None:
    value = await _redis_get(_thumb_key(slide_id, width, height), "get_thumbnail")
    CACHE_REQUESTS.labels(kind="thumbnail", result="hit" if value is not None else "miss").inc()
    return value


async def set_thumbnail(slide_id: str, width: int, height: int, data: bytes) -> None:
    await _redis_set(_thumb_key(slide_id, width, height), data, ttl=settings.thumbnail_cache_ttl)


# ---------------------------------------------------------------------------
# Generic JSON cache (search results, etc.)
# ---------------------------------------------------------------------------

async def get_raw(key: str) -> object | None:
    value = await _redis_get_json(key)
    CACHE_REQUESTS.labels(kind="search", result="hit" if value is not None else "miss").inc()
    return value


async def set_raw(key: str, data: object, ttl: int = 300) -> None:
    await _redis_set_json(key, data, ttl=ttl)


# ---------------------------------------------------------------------------
# Slide metadata cache
# ---------------------------------------------------------------------------

async def get_metadata(slide_id: str) -> dict | None:
    value = await _redis_get_json(_meta_key(slide_id))
    CACHE_REQUESTS.labels(kind="metadata", result="hit" if value is not None else "miss").inc()
    return value


async def set_metadata(slide_id: str, data: dict) -> None:
    await _redis_set_json(_meta_key(slide_id), data, ttl=settings.metadata_cache_ttl)
