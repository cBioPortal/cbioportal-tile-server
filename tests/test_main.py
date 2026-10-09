import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fsspec.exceptions import BlocksizeMismatchError
from tifffile import TiffFileError

import app.main as main_module
from tests.test_auth import (
    SEAL_KEY,
    SEAL_KEY_B64,
    SLIDE_KEY,
    THUMBNAIL_SOURCE,
    TILE_SOURCE,
    make_v4_token,
    seal_claims,
)

WSI_SECRET = "s" * 32


class TestReadinessIdentity:
    def test_release_identity_is_reported_when_complete(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "release_id", "candidate-1")
        monkeypatch.setattr(main_module.settings, "image_git_sha", "a" * 40)
        monkeypatch.setattr(
            main_module.settings, "serving_contract_version", "wsi-serving-v6"
        )
        monkeypatch.setattr(main_module.settings, "wsi_source_seal_key", SEAL_KEY_B64)
        monkeypatch.setattr(
            main_module.settings, "wsi_allowed_source_prefixes", ("s3://pathology/",)
        )
        monkeypatch.setattr(
            main_module.settings,
            "wsi_allowed_thumbnail_prefixes",
            ("s3://mskmind-bkt/wsi-thumbnails/",),
        )
        with patch.object(main_module, "validate_wsi_auth_configuration"):
            status, payload = main_module._readiness_status()

        assert status == 200
        assert payload["release_id"] == "candidate-1"
        assert payload["image_git_sha"] == "a" * 40

    def test_partial_release_identity_fails_readiness(self, monkeypatch):
        monkeypatch.setattr(main_module.settings, "release_id", "candidate-1")
        monkeypatch.setattr(main_module.settings, "image_git_sha", "")
        monkeypatch.setattr(
            main_module.settings, "wsi_allowed_source_prefixes", ("s3://pathology/",)
        )
        monkeypatch.setattr(
            main_module.settings,
            "wsi_allowed_thumbnail_prefixes",
            ("s3://mskmind-bkt/wsi-thumbnails/",),
        )
        with patch.object(main_module, "validate_wsi_auth_configuration"):
            status, payload = main_module._readiness_status()

        assert status == 503
        assert payload["status"] == "unavailable"

    @pytest.mark.parametrize("seal_key", ["", "c2hvcnQ=", SEAL_KEY.hex()])
    def test_invalid_source_seal_key_fails_readiness(self, monkeypatch, seal_key):
        monkeypatch.setattr(main_module.settings, "wsi_auth_secret", WSI_SECRET)
        monkeypatch.setattr(main_module.settings, "wsi_source_seal_key", seal_key)
        monkeypatch.setattr(main_module.settings, "release_id", "")
        monkeypatch.setattr(main_module.settings, "image_git_sha", "")
        monkeypatch.setattr(
            main_module.settings, "wsi_allowed_source_prefixes", ("s3://slides/",)
        )
        monkeypatch.setattr(
            main_module.settings, "wsi_allowed_thumbnail_prefixes", ("s3://thumbs/",)
        )

        status, payload = main_module._readiness_status()

        assert status == 503
        assert payload["status"] == "unavailable"
        if seal_key:
            assert seal_key not in json.dumps(payload)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("seal_key", ["", "c2hvcnQ="])
    async def test_startup_fails_without_valid_source_seal_key(
        self, monkeypatch, caplog, seal_key
    ):
        monkeypatch.setattr(main_module.settings, "wsi_source_seal_key", seal_key)
        init_cache = AsyncMock()
        monkeypatch.setattr(main_module.tile_cache, "init_cache", init_cache)

        with pytest.raises(RuntimeError, match="seal key is not configured"):
            async with main_module.lifespan(main_module.app):
                pass

        init_cache.assert_not_awaited()
        if seal_key:
            assert seal_key not in caplog.text


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

    def test_unclassified_source_oserror_maps_to_retryable_response(self):
        slides = object.__new__(main_module.SlideCache)
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(
                main_module.SlideCache,
                "run",
                side_effect=OSError(main_module.errno.ECONNRESET, "read reset"),
            ),
        ):
            with pytest.raises(main_module.HTTPException) as exc_info:
                main_module._run_slide_operation("s3://bucket/slide.svs", lambda _: None)

        assert exc_info.value.status_code == 503
        assert exc_info.value.headers["Retry-After"] == "1"

    def test_unknown_cache_oserror_is_repaired_and_retried(self):
        slides = object.__new__(main_module.SlideCache)
        error = OSError("cache read failed")
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(main_module.SlideCache, "run", side_effect=[error, b"jpeg"]) as run,
            patch.object(main_module.SlideCache, "repair", return_value=True) as repair,
        ):
            assert main_module._run_slide_operation(
                "s3://bucket/slide.svs", lambda _: None
            ) == b"jpeg"

        assert run.call_count == 2
        repair.assert_called_once_with("s3://bucket/slide.svs")

    @pytest.mark.parametrize("error", [BlocksizeMismatchError("wrong block size"), TiffFileError("bad cache")])
    def test_cache_read_error_is_repaired_and_retried(self, error):
        slides = object.__new__(main_module.SlideCache)
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(main_module.SlideCache, "run", side_effect=[error, b"jpeg"]) as run,
            patch.object(main_module.SlideCache, "repair", return_value=True) as repair,
        ):
            assert main_module._run_slide_operation(
                "s3://bucket/slide.svs", lambda _: None
            ) == b"jpeg"

        assert run.call_count == 2
        repair.assert_called_once_with("s3://bucket/slide.svs")

    def test_cache_read_error_becomes_retryable_after_repair_fails(self):
        slides = object.__new__(main_module.SlideCache)
        error = TiffFileError("bad cache")
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(main_module.SlideCache, "run", side_effect=[error, error]),
            patch.object(main_module.SlideCache, "run_uncached", side_effect=error),
            patch.object(main_module.SlideCache, "repair", return_value=True),
        ):
            with pytest.raises(main_module.HTTPException) as exc_info:
                main_module._run_slide_operation("s3://bucket/slide.svs", lambda _: None)

        assert exc_info.value.status_code == 503
        assert exc_info.value.headers["Retry-After"] == "1"

    def test_decoder_value_error_is_repaired_and_retried(self):
        slides = object.__new__(main_module.SlideCache)
        error = ValueError("decoder rejected cached bytes")
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(main_module.SlideCache, "run", side_effect=[error, b"jpeg"]) as run,
            patch.object(main_module.SlideCache, "repair", return_value=True) as repair,
        ):
            assert main_module._run_slide_operation(
                "s3://bucket/slide.svs", lambda _: None
            ) == b"jpeg"

        assert run.call_count == 2
        repair.assert_called_once_with("s3://bucket/slide.svs")

    def test_cache_repair_failure_is_retryable(self):
        slides = object.__new__(main_module.SlideCache)
        error = ValueError("decoder rejected cached bytes")
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(main_module.SlideCache, "run", side_effect=[error]),
            patch.object(main_module.SlideCache, "run_uncached", side_effect=error),
            patch.object(main_module.SlideCache, "repair", return_value=False),
        ):
            with pytest.raises(main_module.HTTPException) as exc_info:
                main_module._run_slide_operation(
                    "s3://bucket/slide.svs", lambda _: None
                )

        assert exc_info.value.status_code == 503
        assert exc_info.value.headers["Retry-After"] == "1"

    def test_second_cached_failure_uses_direct_reader(self):
        slides = object.__new__(main_module.SlideCache)
        error = ValueError("decoder rejected cached bytes")
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(main_module.SlideCache, "run", side_effect=[error, error]),
            patch.object(main_module.SlideCache, "run_uncached", return_value=b"jpeg") as direct,
            patch.object(main_module.SlideCache, "repair", return_value=True),
        ):
            result = main_module._run_slide_operation(
                "s3://bucket/slide.svs",
                lambda _: None,
                source_fingerprint="a" * 64,
            )

        assert result == b"jpeg"
        direct.assert_called_once()

    def test_fingerprint_is_passed_to_slide_cache_namespace(self):
        slides = object.__new__(main_module.SlideCache)
        with (
            patch.object(main_module, "_slides", slides),
            patch.object(main_module.SlideCache, "run", return_value=b"jpeg") as run,
        ):
            assert main_module._run_slide_operation(
                "s3://bucket/slide.svs",
                lambda _: None,
                source_fingerprint="a" * 64,
            ) == b"jpeg"

        assert run.call_args.kwargs["cache_identity"] == main_module.source_cache_identity(
            "s3://bucket/slide.svs", "a" * 64
        )


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
    async def test_retries_unclassified_transport_oserror(self, monkeypatch):
        record = object()
        monkeypatch.setattr(main_module, "_thumbnail_fetch_semaphore", None)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_max_attempts", 2)
        monkeypatch.setattr(main_module.settings, "thumbnail_fetch_retry_delay_sec", 0)
        reads = [OSError("incomplete ECS read"), b"jpeg"]

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


@pytest.fixture
def wsi_auth_settings(monkeypatch):
    monkeypatch.setattr(main_module.settings, "wsi_auth_secret", WSI_SECRET)
    monkeypatch.setattr(main_module.settings, "wsi_auth_previous_secret", "")
    monkeypatch.setattr(main_module.settings, "wsi_source_seal_key", SEAL_KEY_B64)
    monkeypatch.setattr(main_module, "_source_seal_key", SEAL_KEY)
    monkeypatch.setattr(main_module.settings, "wsi_auth_audience", "cbioportal-wsi")
    monkeypatch.setattr(main_module.settings, "wsi_auth_max_ttl", 300)
    monkeypatch.setattr(main_module.settings, "wsi_allowed_source_schemes", ["s3", "file"])
    monkeypatch.setattr(main_module.settings, "wsi_allowed_source_prefixes", ["s3://slides/"])
    monkeypatch.setattr(main_module.settings, "wsi_allowed_thumbnail_prefixes", ["s3://thumbs/"])


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _request_with_capability(token: str) -> httpx.Request:
    """A tile request carrying the claims the capability middleware would attach."""
    request = httpx.Request("GET", "http://test/tiles/zxy/0/0/0")
    request.state = type("State", (), {})()
    request.state.wsi_claims = main_module.validate_wsi_token(
        token, WSI_SECRET, "cbioportal-wsi", seal_key=SEAL_KEY
    )
    return request


class TestSourceBinding:
    def test_rate_limit_scope_uses_slide_key(self):
        claims = {"study_id": "study-a", "slide_key": SLIDE_KEY, "image_id": "slide-a"}

        assert main_module._slide_rate_limit_scope(claims) == f"study-a\0{SLIDE_KEY}"
        assert "slide-a" not in main_module._slide_rate_limit_scope(claims)

    @pytest.mark.asyncio
    async def test_tile_and_thumbnail_share_slide_rate_limit_scope(self):
        source = "s3://bucket/slide.svs"
        claims = {"sub": "subject", "study_id": "study-a", "slide_key": SLIDE_KEY}
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
                None,
            )
            thumbnail_response = await main_module.thumbnail(
                httpx.Request("GET", "http://test/thumbnails"),
                None,
                256,
                256,
            )

        assert tile_response.status_code == 200
        assert thumbnail_response.status_code == 200
        assert [call.args[3] for call in distributed.await_args_list] == [
            f"study-a\0{SLIDE_KEY}",
            f"study-a\0{SLIDE_KEY}",
        ]

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

    @pytest.mark.parametrize(
        ("operation", "expected"),
        [("tile", TILE_SOURCE), ("thumbnail", THUMBNAIL_SOURCE)],
    )
    def test_authorize_source_uses_decrypted_capability_source(
        self, wsi_auth_settings, operation, expected
    ):
        request = _request_with_capability(make_v4_token(WSI_SECRET))

        source, claims = main_module._authorize_source(request, operation)

        assert source == expected
        assert claims["slide_key"] == SLIDE_KEY

    @pytest.mark.parametrize(
        ("operation", "sealed"),
        [
            # Outside the approved prefix.
            ("tile", {"tile_source": "s3://other/slide-a.svs"}),
            ("thumbnail", {"thumbnail_source": "s3://other/slide-a.jpg"}),
            # Wrong artifact extension for the operation.
            ("tile", {"tile_source": "s3://slides/slide-a.jpg"}),
            ("thumbnail", {"thumbnail_source": "s3://thumbs/slide-a.svs"}),
            # Query strings, traversal and embedded credentials.
            ("tile", {"tile_source": "s3://slides/slide-a.svs?versionId=1"}),
            ("thumbnail", {"thumbnail_source": "s3://thumbs/../private/slide-a.jpg"}),
            ("tile", {"tile_source": "s3://user:pass@slides/slide-a.svs"}),
            # Labelled MRN.
            ("thumbnail", {"thumbnail_source": "s3://thumbs/MRN-12345678.jpg"}),
        ],
    )
    def test_authorize_source_rejects_decrypted_source_outside_policy(
        self, wsi_auth_settings, operation, sealed
    ):
        enc = seal_claims(
            SEAL_KEY,
            SLIDE_KEY,
            {
                "image_id": "slide-a",
                "tile_source": TILE_SOURCE,
                "thumbnail_source": THUMBNAIL_SOURCE,
                **sealed,
            },
        )
        request = _request_with_capability(make_v4_token(WSI_SECRET, enc=enc))

        with pytest.raises(main_module.HTTPException) as exc_info:
            main_module._authorize_source(request, operation)

        assert exc_info.value.status_code in (400, 403)
        assert "s3://" not in str(exc_info.value.detail)
        assert "slide-a" not in str(exc_info.value.detail)

    @pytest.mark.parametrize(
        "source", ["https://slides/slide-a.svs", "gs://slides/slide-a.svs"]
    )
    def test_authorize_source_rejects_decrypted_source_with_unsupported_scheme(
        self, wsi_auth_settings, source
    ):
        enc = seal_claims(
            SEAL_KEY,
            SLIDE_KEY,
            {
                "image_id": "slide-a",
                "tile_source": source,
                "thumbnail_source": THUMBNAIL_SOURCE,
            },
        )
        request = _request_with_capability(make_v4_token(WSI_SECRET, enc=enc))

        with pytest.raises(main_module.HTTPException) as exc_info:
            main_module._authorize_source(request, "tile")

        assert exc_info.value.status_code == 400
        assert "slides" not in str(exc_info.value.detail)


class TestCapabilityRoutes:
    @pytest.mark.asyncio
    async def test_tile_route_serves_decrypted_source(self, wsi_auth_settings):
        get_tile = AsyncMock(return_value=b"tile")
        transport = httpx.ASGITransport(app=main_module.app)
        with patch.object(main_module.tile_cache, "get_tile", new=get_tile):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/tiles/zxy/0/0/0", headers=_bearer(make_v4_token(WSI_SECRET))
                )

        assert response.status_code == 200
        assert response.content == b"tile"
        assert get_tile.await_args.args[0] == main_module.source_cache_identity(TILE_SOURCE)

    @pytest.mark.asyncio
    async def test_thumbnail_route_serves_decrypted_source(self, wsi_auth_settings):
        get_thumbnail = AsyncMock(return_value=b"thumbnail")
        transport = httpx.ASGITransport(app=main_module.app)
        with patch.object(main_module.tile_cache, "get_thumbnail", new=get_thumbnail):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/wsi/thumbnails?width=128&height=96",
                    headers=_bearer(make_v4_token(WSI_SECRET)),
                )

        assert response.status_code == 200
        assert response.content == b"thumbnail"
        assert get_thumbnail.await_args.args[0] == main_module.source_cache_identity(
            THUMBNAIL_SOURCE
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/tiles/zxy/0/0/0", "/thumbnails"])
    @pytest.mark.parametrize(
        ("query", "headers"),
        [
            ("?source=s3%3A%2F%2Fslides%2Fslide-a.svs", {}),
            ("?source=", {}),
            ("", {"X-WSI-Source": "s3://slides/slide-a.svs"}),
            ("", {"x-wsi-source": ""}),
        ],
    )
    async def test_routes_reject_client_supplied_sources(
        self, wsi_auth_settings, path, query, headers
    ):
        transport = httpx.ASGITransport(app=main_module.app)
        with (
            patch.object(main_module.tile_cache, "get_tile", new=AsyncMock(return_value=b"x")),
            patch.object(main_module.tile_cache, "get_thumbnail", new=AsyncMock(return_value=b"x")),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    path + query,
                    headers={**_bearer(make_v4_token(WSI_SECRET)), **headers},
                )

        assert response.status_code == 400
        assert "s3://" not in response.text

    @pytest.mark.asyncio
    async def test_previous_secret_verifies_signature_and_seal_key_opens_enc(
        self, wsi_auth_settings, monkeypatch
    ):
        previous = "p" * 32
        monkeypatch.setattr(main_module.settings, "wsi_auth_previous_secret", previous)
        get_tile = AsyncMock(return_value=b"tile")
        transport = httpx.ASGITransport(app=main_module.app)
        with patch.object(main_module.tile_cache, "get_tile", new=get_tile):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/tiles/zxy/0/0/0", headers=_bearer(make_v4_token(previous))
                )

        assert response.status_code == 200
        assert get_tile.await_args.args[0] == main_module.source_cache_identity(TILE_SOURCE)

    @pytest.mark.asyncio
    async def test_decrypted_thumbnail_outside_policy_is_refused(
        self, wsi_auth_settings
    ):
        enc = seal_claims(
            SEAL_KEY,
            SLIDE_KEY,
            {
                "image_id": "slide-a",
                "tile_source": TILE_SOURCE,
                "thumbnail_source": "s3://other/slide-a.jpg",
            },
        )
        get_thumbnail = AsyncMock(return_value=b"thumbnail")
        transport = httpx.ASGITransport(app=main_module.app)
        with patch.object(main_module.tile_cache, "get_thumbnail", new=get_thumbnail):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    "/thumbnails", headers=_bearer(make_v4_token(WSI_SECRET, enc=enc))
                )

        assert response.status_code == 403
        assert "s3://" not in response.text
        get_thumbnail.assert_not_awaited()
