"""Tests for the Redis cache layer (app/cache.py)."""

from unittest.mock import AsyncMock, patch

import pytest

import app.cache as cache_module


# ---------------------------------------------------------------------------
# No-Redis mode — all operations must be safe no-ops
# ---------------------------------------------------------------------------

class TestNoRedisMode:
    async def test_get_tile_returns_none(self):
        with patch.object(cache_module, "_redis", None):
            assert await cache_module.get_tile("123", 1, 0, 0) is None

    async def test_set_tile_does_not_raise(self):
        with patch.object(cache_module, "_redis", None):
            await cache_module.set_tile("123", 1, 0, 0, b"data")

    async def test_get_thumbnail_returns_none(self):
        with patch.object(cache_module, "_redis", None):
            assert await cache_module.get_thumbnail("123", 256, 256) is None


# ---------------------------------------------------------------------------
# Redis key formatting
# ---------------------------------------------------------------------------

def _make_redis():
    r = AsyncMock()
    r.get    = AsyncMock(return_value=None)
    r.set    = AsyncMock()
    r.setex  = AsyncMock()
    return r


class TestKeyFormats:
    async def test_tile_key(self):
        r = _make_redis()
        with patch.object(cache_module, "_redis", r):
            await cache_module.get_tile("abc", 5, 10, 20)
        r.get.assert_called_once_with("tile:abc:5:10:20")

    async def test_thumbnail_key(self):
        r = _make_redis()
        with patch.object(cache_module, "_redis", r):
            await cache_module.get_thumbnail("abc", 256, 128)
        r.get.assert_called_once_with("thumbnail:abc:256:128")


# ---------------------------------------------------------------------------
# TTL behaviour
# ---------------------------------------------------------------------------

class TestTtlBehaviour:
    async def test_thumbnail_cache_uses_configured_ttl(self, monkeypatch):
        r = _make_redis()
        monkeypatch.setattr(cache_module.settings, "thumbnail_cache_ttl", 3600)
        with patch.object(cache_module, "_redis", r):
            await cache_module.set_thumbnail("123", 256, 128, b"thumb")
        r.setex.assert_called_once_with("thumbnail:123:256:128", 3600, b"thumb")

class TestRedisCircuitBreaker:
    async def test_timeout_bypasses_subsequent_requests_during_backoff(self, monkeypatch):
        r = _make_redis()
        r.get = AsyncMock(side_effect=TimeoutError("redis unavailable"))
        monkeypatch.setattr(cache_module.settings, "redis_failure_backoff_seconds", 5)
        monkeypatch.setattr(cache_module, "_redis_disabled_until", 0.0)
        monkeypatch.setattr(cache_module, "_redis_failure_logged", False)

        with patch.object(cache_module, "_redis", r):
            assert await cache_module.get_tile("abc", 1, 0, 0) is None
            assert await cache_module.get_tile("abc", 1, 0, 0) is None

        r.get.assert_awaited_once()
