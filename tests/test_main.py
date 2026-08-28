import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

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

    @pytest.mark.asyncio
    async def test_queue_timeout_rejects_work_without_waiting_for_a_slot(self, monkeypatch):
        main_module._image_operation_semaphore = asyncio.Semaphore(1)
        await main_module._image_operation_semaphore.acquire()
        monkeypatch.setattr(main_module.settings, "image_operation_queue_timeout_seconds", 0.1)

        with pytest.raises(main_module.ImageOperationQueueTimeout):
            await main_module._run_image_operation(lambda: b"never-starts")

        main_module._image_operation_semaphore.release()


class TestTransientSourceFailures:
    def test_slide_source_timeout_maps_to_retryable_response(self):
        slides = object.__new__(main_module.SlideCache)
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(
                main_module.SlideCache,
                "run",
                side_effect=EndpointConnectionError(endpoint_url="http://ecs:9020"),
            ),
        ):
            with pytest.raises(main_module.HTTPException) as exc_info:
                main_module._run_slide_operation("s3://bucket/slide.svs", lambda _: None)

        assert exc_info.value.status_code == 503
        assert exc_info.value.headers["Retry-After"] == "1"


class TestThumbnailFetchRetry:
    @pytest.mark.asyncio
    async def test_retries_transient_object_read(self, monkeypatch):
        record = object()
        monkeypatch.setattr(main_module, "_thumbnail_fetch_semaphore", None)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_max_attempts", 2)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_retry_delay_sec", 0)
        reads = [EndpointConnectionError(endpoint_url="http://ecs:9020"), b"jpeg"]

        async def fake_in_thread(fn, *args):
            value = reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(main_module, "_in_thread", fake_in_thread):
            assert await main_module._run_thumbnail_fetch(record) == b"jpeg"
        assert reads == []

    @pytest.mark.asyncio
    async def test_does_not_retry_missing_object(self, monkeypatch):
        record = object()
        monkeypatch.setattr(main_module, "_thumbnail_fetch_semaphore", None)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_max_attempts", 2)
        read_calls = 0

        async def fake_in_thread(fn, *args):
            nonlocal read_calls
            read_calls += 1
            raise FileNotFoundError("missing")

        with patch.object(main_module, "_in_thread", fake_in_thread):
            with pytest.raises(FileNotFoundError):
                await main_module._run_thumbnail_fetch(record)
        assert read_calls == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            PermissionError("access denied"),
            ValueError("malformed thumbnail"),
            ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": "denied"},
                    "ResponseMetadata": {"HTTPStatusCode": 403},
                },
                "GetObject",
            ),
        ],
    )
    async def test_does_not_retry_terminal_object_read_errors(
        self, monkeypatch, error
    ):
        record = object()
        monkeypatch.setattr(main_module, "_thumbnail_fetch_semaphore", None)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_max_attempts", 2)
        read_calls = 0

        async def fake_in_thread(fn, *args):
            nonlocal read_calls
            read_calls += 1
            raise error

        with patch.object(main_module, "_in_thread", fake_in_thread):
            with pytest.raises(type(error)):
                await main_module._run_thumbnail_fetch(record)
        assert read_calls == 1

    @pytest.mark.asyncio
    async def test_retries_s3_server_error(self, monkeypatch):
        record = object()
        monkeypatch.setattr(main_module, "_thumbnail_fetch_semaphore", None)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_max_attempts", 2)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_retry_delay_sec", 0)
        reads = [
            ClientError(
                {
                    "Error": {"Code": "InternalError", "Message": "try again"},
                    "ResponseMetadata": {"HTTPStatusCode": 500},
                },
                "GetObject",
            ),
            b"jpeg",
        ]

        async def fake_in_thread(fn, *args):
            value = reads.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        with patch.object(main_module, "_in_thread", fake_in_thread):
            assert await main_module._run_thumbnail_fetch(record) == b"jpeg"
        assert reads == []


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
    async def test_private_network_preflight_allows_browser_access(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs",
                headers={
                    "Origin": "https://cbioportal.mskcc.org",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                    "Access-Control-Request-Private-Network": "true",
                },
            )

        assert response.status_code == 200
        assert response.headers["access-control-allow-private-network"] == "true"
    @pytest.mark.asyncio
    async def test_private_network_preflight_still_rejects_unknown_origin(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs",
                headers={
                    "Origin": "https://untrusted.example",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                    "Access-Control-Request-Private-Network": "true",
                },
            )

        assert response.status_code == 400
        assert response.text == "Disallowed CORS origin"
        assert "access-control-allow-origin" not in response.headers

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


class TestSourceBinding:
    def test_source_header_is_used_when_query_is_absent(self):
        request = httpx.Request(
            "GET",
            "http://test/thumbnails",
            headers={"X-WSI-Source": "s3://bucket/thumbnail.jpg"},
        )

        assert main_module._source_from_request(request, None) == "s3://bucket/thumbnail.jpg"

    def test_query_source_remains_supported(self):
        request = httpx.Request("GET", "http://test/thumbnails")

        assert main_module._source_from_request(request, "s3://bucket/thumbnail.jpg") == (
            "s3://bucket/thumbnail.jpg"
        )

    def test_matching_header_and_query_source_are_accepted(self):
        request = httpx.Request(
            "GET",
            "http://test/thumbnails",
            headers={"X-WSI-Source": "s3://bucket/thumbnail.jpg"},
        )

        assert main_module._source_from_request(
            request, "s3://bucket/thumbnail.jpg"
        ) == "s3://bucket/thumbnail.jpg"

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
    async def test_tile_route_accepts_header_only_source(self):
        request = httpx.Request(
            "GET",
            "http://test/tiles/zxy/0/0/0",
            headers={"X-WSI-Source": "s3://bucket/slide.svs"},
        )
        cached_tile = b"tile"

        with (
            patch.object(
                main_module,
                "_authorize_source",
                return_value=("s3://bucket/slide.svs", {"sub": "test-user"}),
            ) as authorize,
            patch.object(
                main_module.tile_cache,
                "get_tile",
                new=AsyncMock(return_value=cached_tile),
            ),
        ):
            response = await main_module.tile(request, 0, 0, 0, None)

        authorize.assert_called_once_with(request, "s3://bucket/slide.svs", "tile")
        assert response.body == cached_tile

    @pytest.mark.asyncio
    async def test_thumbnail_route_accepts_header_only_source(self):
        request = httpx.Request(
            "GET",
            "http://test/thumbnails",
            headers={"X-WSI-Source": "s3://bucket/thumbnail.jpg"},
        )
        cached_thumbnail = b"thumbnail"

        with (
            patch.object(
                main_module,
                "_authorize_source",
                return_value=("s3://bucket/thumbnail.jpg", {"sub": "test-user"}),
            ) as authorize,
            patch.object(
                main_module.tile_cache,
                "get_thumbnail",
                new=AsyncMock(return_value=cached_thumbnail),
            ),
        ):
            response = await main_module.thumbnail(request, None, 128, 96)

        authorize.assert_called_once_with(request, "s3://bucket/thumbnail.jpg", "thumbnail")
        assert response.body == cached_thumbnail
