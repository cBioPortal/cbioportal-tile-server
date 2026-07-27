import asyncio
from unittest.mock import patch

import pytest

import app.main as main_module
from app.config import settings


class TestSingleFlight:
    @pytest.mark.asyncio
    async def test_identical_cache_misses_share_one_decode(self):
        singleflight = main_module._SingleFlight()
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return b"jpeg"

        results = await asyncio.gather(
            *[singleflight.do("tile:1:0:0:0", "tile", producer) for _ in range(5)]
        )

        assert results == [b"jpeg"] * 5
        assert calls == 1


class TestImageOperationGate:
    @pytest.mark.asyncio
    async def test_distinct_requests_respect_image_operation_cap(self):
        active = 0
        peak = 0

        async def fake_in_thread(fn, *args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return fn(*args)

        main_module._image_operation_semaphore = asyncio.Semaphore(2)

        with patch.object(main_module, "_in_thread", fake_in_thread):
            results = await asyncio.gather(
                *[main_module._run_image_operation(lambda value=value: value) for value in range(5)]
            )

        assert results == [0, 1, 2, 3, 4]
        assert peak == 2


class TestPathCache:
    def test_path_cache_evicts_least_recently_used_entry(self, monkeypatch):
        main_module._path_cache.clear()
        monkeypatch.setattr(settings, "path_cache_capacity", 2)

        with patch("app.main.meta.get_slide_path", side_effect=lambda image_id, _: f"s3://bucket/{image_id}.svs"):
            assert main_module._resolve_slide_id("a") == "s3://bucket/a.svs"
            assert main_module._resolve_slide_id("b") == "s3://bucket/b.svs"
            assert main_module._resolve_slide_id("a") == "s3://bucket/a.svs"
            assert main_module._resolve_slide_id("c") == "s3://bucket/c.svs"

        assert list(main_module._path_cache.keys()) == ["a", "c"]
