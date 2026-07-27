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

import redis.asyncio as aioredis

from .config import settings

_redis: aioredis.Redis | None = None


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
    global _redis
    if not _redis_configured():
        return  # no cache configured — all get/set calls are no-ops
    _redis = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=False,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


async def close_cache() -> None:
    if _redis:
        await _redis.aclose()


# ---------------------------------------------------------------------------
# Internal I/O primitives — single guard + try/except for all get/set paths
# ---------------------------------------------------------------------------

async def _redis_get(key: str) -> bytes | None:
    """Guarded GET; returns None when cache is unavailable or on error."""
    if not _redis:
        return None
    try:
        return await _redis.get(key)
    except Exception:
        return None


async def _redis_set(key: str, data: bytes | str, ttl: int = 0) -> None:
    """Guarded SET/SETEX; silently swallows errors so cache is never fatal."""
    if not _redis:
        return
    try:
        if ttl:
            await _redis.setex(key, ttl, data)
        else:
            await _redis.set(key, data)
    except Exception:
        pass


def _from_json(raw: bytes | None) -> object | None:
    return json.loads(raw) if raw else None


async def _redis_get_json(key: str) -> object | None:
    return _from_json(await _redis_get(key))


async def _redis_set_json(key: str, data: object, ttl: int = 0) -> None:
    await _redis_set(key, json.dumps(data, default=str), ttl=ttl)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_tile(slide_id: str, z: int, x: int, y: int) -> bytes | None:
    return await _redis_get(_tile_key(slide_id, z, x, y))


async def set_tile(slide_id: str, z: int, x: int, y: int, data: bytes) -> None:
    await _redis_set(_tile_key(slide_id, z, x, y), data, ttl=settings.tile_cache_ttl)


async def get_thumbnail(slide_id: str, width: int, height: int) -> bytes | None:
    return await _redis_get(_thumb_key(slide_id, width, height))


async def set_thumbnail(slide_id: str, width: int, height: int, data: bytes) -> None:
    await _redis_set(_thumb_key(slide_id, width, height), data, ttl=settings.thumbnail_cache_ttl)


# ---------------------------------------------------------------------------
# Generic JSON cache (search results, etc.)
# ---------------------------------------------------------------------------

async def get_raw(key: str) -> object | None:
    return await _redis_get_json(key)


async def set_raw(key: str, data: object, ttl: int = 300) -> None:
    await _redis_set_json(key, data, ttl=ttl)


# ---------------------------------------------------------------------------
# Slide metadata cache
# ---------------------------------------------------------------------------

async def get_metadata(slide_id: str) -> dict | None:
    return await _redis_get_json(_meta_key(slide_id))


async def set_metadata(slide_id: str, data: dict) -> None:
    await _redis_set_json(_meta_key(slide_id), data, ttl=settings.metadata_cache_ttl)
