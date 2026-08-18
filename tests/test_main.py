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

    @pytest.mark.asyncio
    async def test_distributed_owner_rechecks_cache_before_extracting(self):
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            return b"generated"

        async def cached():
            return b"already-published"

        with (
            patch.object(main_module.tile_cache, "try_acquire_miss_lock", return_value="token"),
            patch.object(main_module.tile_cache, "release_miss_lock") as release,
            patch.object(main_module.tile_cache, "allow_cache_miss") as allow,
        ):
            result = await main_module._distributed_singleflight(
                "tile:race-check",
                "tile",
                "subject",
                producer,
                cached,
                lambda value: value,
            )

        assert result == b"already-published"
        assert calls == 0
        allow.assert_not_awaited()
        release.assert_awaited_once_with("tile:race-check", "token")


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

    @pytest.mark.asyncio
    async def test_thumbnail_work_uses_image_operation_gate(self):
        active = 0
        peak = 0

        async def fake_in_thread(fn, *args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return fn(*args)

        main_module._image_operation_semaphore = asyncio.Semaphore(1)
        with patch.object(main_module, "_in_thread", fake_in_thread):
            results = await asyncio.gather(
                *[
                    main_module._run_image_operation(
                        lambda value=value: value,
                        operation_kind="thumbnail",
                    )
                    for value in range(4)
                ]
            )

        assert results == [0, 1, 2, 3]
        assert peak == 1


class TestCorsPreflight:
    @pytest.mark.asyncio
    async def test_preflight_does_not_require_slide_capability(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/tiles/zxy/0/0/0",
                headers={
                    "Origin": "https://cbioportal.mskcc.org",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization, X-WSI-Source",
                },
            )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://cbioportal.mskcc.org"
        assert "authorization" in response.headers["access-control-allow-headers"].lower()
        assert "x-wsi-source" in response.headers["access-control-allow-headers"].lower()

    @pytest.mark.asyncio
    async def test_unauthenticated_tile_get_remains_protected(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/tiles/zxy/0/0/0",
                headers={"Origin": "https://cbioportal.mskcc.org"},
            )

        assert response.status_code == 401

    def test_source_must_be_supplied_in_header(self):
        scope = {
            "type": "http",
            "path": "/tiles/zxy/0/0/0",
            "query_string": b"",
            "headers": [],
            "method": "GET",
        }
        with pytest.raises(main_module.HTTPException, match="X-WSI-Source"):
            main_module._request_source(main_module.Request(scope, receive=lambda: None))

    def test_source_query_parameter_is_rejected(self):
        scope = {
            "type": "http",
            "path": "/tiles/zxy/0/0/0",
            "query_string": b"source=s3%3A%2F%2Fbucket%2Fslide.svs",
            "headers": [(b"x-wsi-source", b"s3://bucket/slide.svs")],
            "method": "GET",
        }
        with pytest.raises(main_module.HTTPException, match="query parameter"):
            main_module._request_source(main_module.Request(scope, receive=lambda: None))

    @pytest.mark.asyncio
    async def test_metrics_bypasses_capability_guard(self):
        scope = {
            "type": "http",
            "path": "/metrics",
            "headers": [],
            "method": "GET",
        }
        starlette_request = main_module.Request(scope, receive=lambda: None)

        async def call_next(request):
            return main_module.Response(status_code=204)

        response = await main_module.require_wsi_capability(starlette_request, call_next)
        assert response.status_code == 204
