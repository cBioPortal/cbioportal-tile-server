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
    r.eval   = AsyncMock(return_value=1)
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

class TestMissLimitKey:
    def test_miss_limit_key_is_scoped_by_slide(self):
        first = cache_module._miss_limit_key("subject", "slide-a")
        same = cache_module._miss_limit_key("subject", "slide-a")
        other_slide = cache_module._miss_limit_key("subject", "slide-b")
        other_subject = cache_module._miss_limit_key("other", "slide-a")

        assert first == same
        assert first != other_slide
        assert first != other_subject
        assert "slide-a" not in first


class TestMissLock:
    async def test_renew_miss_lock_extends_only_owned_lock(self, monkeypatch):
        r = _make_redis()
        monkeypatch.setattr(cache_module.settings, "cache_miss_lock_ttl_seconds", 120)
        with patch.object(cache_module, "_redis", r):
            renewed = await cache_module.renew_miss_lock("tile:abc", "token")

        assert renewed is True
        r.eval.assert_awaited_once_with(
            cache_module._RENEW_LOCK_SCRIPT,
            1,
            "lock:tile:abc",
            "token",
            120_000,
        )


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
