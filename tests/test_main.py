import asyncio
from unittest.mock import patch

import httpx
import pytest

import app.main as main_module


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


class TestCorsPreflight:
    @pytest.mark.asyncio
    async def test_preflight_does_not_require_slide_capability(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs",
                headers={
                    "Origin": "https://cbioportal.mskcc.org",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://cbioportal.mskcc.org"
        assert "authorization" in response.headers["access-control-allow-headers"].lower()

    @pytest.mark.asyncio
    async def test_unauthenticated_tile_get_remains_protected(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs",
                headers={"Origin": "https://cbioportal.mskcc.org"},
            )

        assert response.status_code == 401
