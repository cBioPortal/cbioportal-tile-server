"""
Redis tile cache.

Keys:  tile:{source_digest}:{z}:{x}:{y}
Value: raw JPEG bytes

Tiles are immutable so tile TTL is configured separately. A thumbnail cache
uses the key thumbnail:{source_digest}:{width}:{height}.

Patient hierarchy and all clinical metadata are owned by the cBioPortal
backend. This cache is limited to source-bound slide tiles and thumbnails.
"""

import json
import hashlib
import logging
import time
import uuid

import redis.asyncio as aioredis

from .config import settings
from .metrics import (
    CACHE_MISS_RATE_LIMITS,
    CACHE_REQUESTS,
    DISTRIBUTED_MISS_LOCKS,
    REDIS_ERRORS,
    REDIS_OPERATION_SECONDS,
)

_redis: aioredis.Redis | None = None
_redis_disabled_until = 0.0
_redis_failure_logged = False
logger = logging.getLogger(__name__)

_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_CACHE_MISS_LIMIT_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('zremrangebyscore', KEYS[1], 0, now - window)
local count = redis.call('zcard', KEYS[1])
if count >= limit then
  local first = redis.call('zrange', KEYS[1], 0, 0, 'WITHSCORES')
  return {0, first[2]}
end
redis.call('zadd', KEYS[1], now, member)
redis.call('expire', KEYS[1], math.ceil(window / 1000) + 1)
return {1, now}
"""


# ---------------------------------------------------------------------------
# Key helpers — single source of truth for all cache key formats
# ---------------------------------------------------------------------------

def _tile_key(slide_id: str, z: int, x: int, y: int) -> str:
    return f"tile:{slide_id}:{z}:{x}:{y}"

def _thumb_key(slide_id: str, width: int, height: int) -> str:
    return f"thumbnail:{slide_id}:{width}:{height}"

def _meta_key(slide_id: str) -> str:
    return f"meta:{slide_id}"


def _thumbnail_status_key(slide_id: str, width: int, height: int) -> str:
    return f"thumbnail-status:{slide_id}:{width}:{height}"


def tile_cache_key(slide_id: str, z: int, x: int, y: int) -> str:
    return _tile_key(slide_id, z, x, y)


def thumbnail_cache_key(slide_id: str, width: int, height: int) -> str:
    return _thumb_key(slide_id, width, height)


def _lock_key(cache_key: str) -> str:
    return f"lock:{cache_key}"


def _miss_limit_key(subject: str, scope: str) -> str:
    digest = hashlib.sha256(f"{subject}\0{scope}".encode("utf-8")).hexdigest()
    return f"rate:cache-miss:{digest}"


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


async def try_acquire_miss_lock(cache_key: str, kind: str) -> str | bool | None:
    """Return token for owner, False for another owner, or None if Redis is down."""
    if not _redis_available():
        return None
    token = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        acquired = await _redis.set(
            _lock_key(cache_key),
            token,
            nx=True,
            px=max(1, settings.cache_miss_lock_ttl_seconds) * 1000,
        )
        REDIS_OPERATION_SECONDS.labels(operation="acquire_miss_lock").observe(
            time.perf_counter() - started
        )
        _record_redis_recovery()
        result = token if acquired else False
        DISTRIBUTED_MISS_LOCKS.labels(kind=kind, result="owner" if acquired else "follower").inc()
        return result
    except Exception:
        REDIS_OPERATION_SECONDS.labels(operation="acquire_miss_lock").observe(
            time.perf_counter() - started
        )
        REDIS_ERRORS.labels(operation="acquire_miss_lock").inc()
        DISTRIBUTED_MISS_LOCKS.labels(kind=kind, result="unavailable").inc()
        _trip_redis_breaker("acquire_miss_lock")
        return None


async def release_miss_lock(cache_key: str, token: str) -> None:
    if not _redis_available():
        return
    started = time.perf_counter()
    try:
        await _redis.eval(_RELEASE_LOCK_SCRIPT, 1, _lock_key(cache_key), token)
        REDIS_OPERATION_SECONDS.labels(operation="release_miss_lock").observe(
            time.perf_counter() - started
        )
        _record_redis_recovery()
    except Exception:
        REDIS_OPERATION_SECONDS.labels(operation="release_miss_lock").observe(
            time.perf_counter() - started
        )
        REDIS_ERRORS.labels(operation="release_miss_lock").inc()
        _trip_redis_breaker("release_miss_lock")


async def renew_miss_lock(cache_key: str, token: str) -> bool:
    """Extend an owned miss lock without changing ownership."""
    if not _redis_available():
        return False
    started = time.perf_counter()
    try:
        renewed = await _redis.eval(
            _RENEW_LOCK_SCRIPT,
            1,
            _lock_key(cache_key),
            token,
            max(1, settings.cache_miss_lock_ttl_seconds) * 1000,
        )
        REDIS_OPERATION_SECONDS.labels(operation="renew_miss_lock").observe(
            time.perf_counter() - started
        )
        _record_redis_recovery()
        return bool(int(renewed))
    except Exception:
        REDIS_OPERATION_SECONDS.labels(operation="renew_miss_lock").observe(
            time.perf_counter() - started
        )
        REDIS_ERRORS.labels(operation="renew_miss_lock").inc()
        _trip_redis_breaker("renew_miss_lock")
        return False


async def allow_cache_miss(subject: str, scope: str) -> tuple[bool, int]:
    """Atomically apply the shared per-source cache-miss limit and return (allowed, retry seconds)."""
    limit = settings.cache_miss_rate_limit_per_minute
    if limit <= 0 or not _redis_available():
        CACHE_MISS_RATE_LIMITS.labels(result="bypassed").inc()
        return True, 0
    now_ms = int(time.time() * 1000)
    window_ms = 60_000
    member = f"{now_ms}:{uuid.uuid4().hex}"
    started = time.perf_counter()
    try:
        result = await _redis.eval(
            _CACHE_MISS_LIMIT_SCRIPT,
            1,
            _miss_limit_key(subject, scope),
            now_ms,
            window_ms,
            limit,
            member,
        )
        REDIS_OPERATION_SECONDS.labels(operation="cache_miss_limit").observe(
            time.perf_counter() - started
        )
        _record_redis_recovery()
        allowed = bool(int(result[0]))
        if allowed:
            CACHE_MISS_RATE_LIMITS.labels(result="allowed").inc()
            return True, 0
        first_ms = int(result[1])
        retry_after = max(1, int((first_ms + window_ms - now_ms + 999) / 1000))
        CACHE_MISS_RATE_LIMITS.labels(result="rejected").inc()
        return False, retry_after
    except Exception:
        REDIS_OPERATION_SECONDS.labels(operation="cache_miss_limit").observe(
            time.perf_counter() - started
        )
        REDIS_ERRORS.labels(operation="cache_miss_limit").inc()
        CACHE_MISS_RATE_LIMITS.labels(result="unavailable").inc()
        _trip_redis_breaker("cache_miss_limit")
        return True, 0


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


async def set_thumbnail(
    slide_id: str,
    width: int,
    height: int,
    data: bytes,
    ttl: int | None = None,
) -> None:
    await _redis_set(
        _thumb_key(slide_id, width, height),
        data,
        ttl=settings.thumbnail_cache_ttl if ttl is None else ttl,
    )


async def get_thumbnail_status(slide_id: str, width: int, height: int) -> dict | None:
    value = await _redis_get_json(_thumbnail_status_key(slide_id, width, height))
    return value if isinstance(value, dict) else None


async def set_thumbnail_status(
    slide_id: str,
    width: int,
    height: int,
    status: dict,
    ttl: int | None = None,
) -> None:
    await _redis_set_json(
        _thumbnail_status_key(slide_id, width, height),
        status,
        ttl=settings.thumbnail_cache_ttl if ttl is None else ttl,
    )


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
