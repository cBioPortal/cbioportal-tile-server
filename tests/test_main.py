import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.main as main_module


class TestSingleFlight:
    @pytest.mark.asyncio
    async def test_long_owner_renews_distributed_lock(self, monkeypatch):
        renew = AsyncMock(return_value=True)
        monkeypatch.setattr(main_module.settings, "cache_miss_lock_ttl_seconds", 3)

        async def producer():
            await asyncio.sleep(1.05)
            return b"generated"

        with patch.object(main_module.tile_cache, "renew_miss_lock", renew):
            result = await main_module._run_with_miss_lock_lease(
                "tile:long", "token", producer
            )

        assert result == b"generated"
        renew.assert_awaited()

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
                "slide-a",
                producer,
                cached,
                lambda value: value,
            )

        assert result == b"already-published"
        assert calls == 0
        allow.assert_not_awaited()
        release.assert_awaited_once_with("tile:race-check", "token")

    @pytest.mark.asyncio
    async def test_cache_miss_limit_uses_source_scope(self):
        async def producer():
            return b"generated"

        async def read_cached():
            return None

        with (
            patch.object(main_module.tile_cache, "try_acquire_miss_lock", return_value="token"),
            patch.object(main_module.tile_cache, "release_miss_lock"),
            patch.object(main_module.tile_cache, "allow_cache_miss", return_value=(True, 0)) as allow,
            patch.object(main_module.tile_cache, "set_tile"),
        ):
            result = await main_module._distributed_singleflight(
                "tile:slide-a:4:0:0",
                "tile",
                "subject",
                "slide-a",
                producer,
                read_cached,
                lambda value: value,
            )

        assert result == b"generated"
        allow.assert_awaited_once_with("subject", "slide-a")


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


class TestSourceBinding:
    def test_source_header_is_preferred_when_query_is_absent(self):
        request = httpx.Request(
            "GET",
            "http://test/thumbnails",
            headers={"X-WSI-Source": "s3://bucket/thumbnail.jpg"},
        )
        assert main_module._source_from_request(request, None) == "s3://bucket/thumbnail.jpg"

    def test_rate_limit_scope_is_shared_by_tile_and_thumbnail_sources(self):
        claims = {"study_id": "study-a", "image_id": "slide-a"}

        assert main_module._slide_rate_limit_scope(claims) == "study-a\0slide-a"

    @pytest.mark.asyncio
    async def test_tile_and_thumbnail_share_slide_rate_limit_scope(self):
        source = "s3://bucket/slide.svs"
        claims = {"sub": "subject", "study_id": "study-a", "image_id": "slide-a"}
        distributed_results = [
            b"jpeg",
            (b"jpeg", {"status": "ok", "reason": "test"}),
        ]

        with (
            patch.object(main_module, "_authorize_source", return_value=(source, claims)),
            patch.object(main_module.tile_cache, "get_tile", return_value=None),
            patch.object(main_module.tile_cache, "get_thumbnail", return_value=None),
            patch.object(
                main_module,
                "_distributed_singleflight",
                side_effect=distributed_results,
            ) as distributed,
        ):
            tile_response = await main_module.tile(
                httpx.Request("GET", "http://test/tiles/zxy/0/0/0"),
                0,
                0,
                0,
                source,
            )
            thumbnail_response = await main_module.thumbnail(
                httpx.Request("GET", "http://test/thumbnails"),
                source,
                256,
                256,
            )

        assert tile_response.status_code == 200
        assert thumbnail_response.status_code == 200
        assert [call.args[3] for call in distributed.await_args_list] == [
            "study-a\0slide-a",
            "study-a\0slide-a",
        ]

    def test_conflicting_source_bindings_are_rejected(self):
        request = httpx.Request(
            "GET",
            "http://test/thumbnails",
            headers={"X-WSI-Source": "s3://bucket/header.jpg"},
        )
        with pytest.raises(main_module.HTTPException) as exc_info:
            main_module._source_from_request(request, "s3://bucket/query.jpg")
        assert exc_info.value.status_code == 400

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
