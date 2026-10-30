import asyncio
import json
from io import BytesIO
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fsspec.exceptions import BlocksizeMismatchError
from PIL import Image
from tifffile import TiffFileError

import app.auth as auth_module
import app.main as main_module
from tests.test_auth import (
    SEAL_KEY,
    SEAL_KEY_B64,
    SLIDE_KEY,
    THUMBNAIL_SOURCE,
    TILE_SOURCE,
    make_enc,
)


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
        monkeypatch.setattr(main_module, "_source_seal_key", SEAL_KEY)
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
        monkeypatch.setattr(main_module, "_source_seal_key", SEAL_KEY)
        status, payload = main_module._readiness_status()

        assert status == 503
        assert payload["status"] == "unavailable"

    @pytest.mark.parametrize("seal_key", [None, b"short", SEAL_KEY.hex().encode()])
    def test_invalid_source_seal_key_fails_readiness(self, monkeypatch, seal_key):
        monkeypatch.setattr(main_module, "_source_seal_key", seal_key)
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
            assert seal_key.decode() not in json.dumps(payload)

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

    def test_source_replaced_in_place_is_reopened_not_retried(self):
        from s3fs.utils import FileExpired

        slides = object.__new__(main_module.SlideCache)
        # FileExpired is an EBUSY OSError, which would otherwise read as transient.
        error = FileExpired(filename="bucket/slide.svs", e_tag='"old"')
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
            )

        assert result == b"jpeg"
        direct.assert_called_once()


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
    async def test_preflight_is_answered_without_slide_parameters(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs",
                headers={
                    "Origin": "https://cbioportal.mskcc.org",
                    "Access-Control-Request-Method": "GET",
                },
            )

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://cbioportal.mskcc.org"
    @pytest.mark.asyncio
    async def test_private_network_preflight_allows_browser_access(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.options(
                "/tiles/zxy/0/0/0?source=s3%3A%2F%2Fbucket%2Fslide.svs",
                headers={
                    "Origin": "https://cbioportal.mskcc.org",
                    "Access-Control-Request-Method": "GET",
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
                    "Access-Control-Request-Private-Network": "true",
                },
            )

        assert response.status_code == 400
        assert response.text == "Disallowed CORS origin"
        assert "access-control-allow-origin" not in response.headers

    @pytest.mark.asyncio
    async def test_tile_get_without_slide_parameters_is_rejected(self):
        transport = httpx.ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/tiles/zxy/0/0/0",
                headers={"Origin": "https://cbioportal.mskcc.org"},
            )

        assert response.status_code == 400


@pytest.fixture
def slide_settings(monkeypatch):
    auth_module._decrypt_sealed_claims_cached.cache_clear()
    monkeypatch.setattr(main_module.settings, "wsi_source_seal_key", SEAL_KEY_B64)
    monkeypatch.setattr(main_module, "_source_seal_key", SEAL_KEY)
    monkeypatch.setattr(main_module.settings, "wsi_allowed_source_schemes", ["s3", "file"])
    monkeypatch.setattr(main_module.settings, "wsi_allowed_source_prefixes", ["s3://slides/"])
    monkeypatch.setattr(main_module.settings, "wsi_allowed_thumbnail_prefixes", ["s3://thumbs/"])


STUDY_ID = "study_a"
OTHER_SLIDE_KEY = "ba9876543210"


def _query(**overrides) -> dict:
    params = {"studyId": STUDY_ID, "key": SLIDE_KEY, "enc": make_enc()}
    params.update(overrides)
    return {name: value for name, value in params.items() if value is not None}


def _thumbnail_query(**overrides) -> dict:
    return _query(**{"width": "128", "height": "96", **overrides})


def _jpeg(size: tuple[int, int]) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, (120, 140, 160)).save(out, format="JPEG")
    return out.getvalue()


async def _get(path: str, params=None, headers=None) -> httpx.Response:
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, params=params, headers=headers)


class TestSlideRoutes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "params", "cache_fn", "expected", "cache_control"),
        [
            ("/tiles/zxy/0/0/0", _query, "get_tile", TILE_SOURCE, "private, max-age=3600"),
            ("/wsi/tiles/zxy/0/0/0", _query, "get_tile", TILE_SOURCE, "private, max-age=3600"),
            ("/thumbnails", _thumbnail_query, "get_thumbnail", THUMBNAIL_SOURCE, "private, max-age=300"),
            ("/wsi/thumbnails", _thumbnail_query, "get_thumbnail", THUMBNAIL_SOURCE, "private, max-age=300"),
        ],
    )
    async def test_routes_serve_the_sealed_source(
        self, slide_settings, path, params, cache_fn, expected, cache_control
    ):
        cached = AsyncMock(return_value=b"image")
        with patch.object(main_module.tile_cache, cache_fn, new=cached):
            response = await _get(path, params())

        assert response.status_code == 200
        assert response.content == b"image"
        assert cached.await_args.args[0] == main_module.source_digest(expected)
        assert response.headers["cache-control"] == cache_control
        assert response.headers["vary"] == "Cookie"

    async def _serve_thumbnail(self, params, master: bytes):
        """Serve one thumbnail miss; return (response, fetched records, cache writes)."""
        records = []
        stored = AsyncMock()

        async def fetch(record):
            records.append(record)
            return master

        async def run_image_operation(fn, *args, **kwargs):
            return fn(*args)

        with (
            patch.object(main_module.tile_cache, "get_thumbnail", new=AsyncMock(return_value=None)),
            patch.object(main_module.tile_cache, "set_thumbnail", new=stored),
            patch.object(main_module.tile_cache, "try_acquire_miss_lock", new=AsyncMock(return_value=None)),
            patch.object(main_module, "_run_thumbnail_fetch", side_effect=fetch),
            patch.object(main_module, "_run_image_operation", side_effect=run_image_operation),
        ):
            response = await _get("/thumbnails", params)
        return response, records, [call.args for call in stored.await_args_list]

    @pytest.mark.asyncio
    async def test_thumbnail_resizes_by_the_stored_thumbnail_size(self, slide_settings):
        response, records, writes = await self._serve_thumbnail(
            _thumbnail_query(), _jpeg((1024, 768))
        )

        assert response.status_code == 200
        assert response.headers["x-thumbnail-reason"] == "resized"
        assert Image.open(BytesIO(response.content)).size == (128, 96)
        assert [r.uri for r in records] == [THUMBNAIL_SOURCE]
        assert [args[:3] for args in writes] == [
            (main_module.source_digest(THUMBNAIL_SOURCE), 128, 96)
        ]

    @pytest.mark.asyncio
    async def test_thumbnail_passes_a_master_that_fits_through(self, slide_settings):
        master = _jpeg((100, 50))
        response, _, writes = await self._serve_thumbnail(_thumbnail_query(), master)

        assert response.status_code == 200
        assert response.headers["x-thumbnail-reason"] == "master"
        assert response.content == master
        assert writes[0][3] == master

    @pytest.mark.asyncio
    async def test_thumbnail_reencodes_a_png_master_to_jpeg(self, slide_settings):
        out = BytesIO()
        Image.new("RGB", (100, 50), (120, 140, 160)).save(out, format="PNG")
        response, _, writes = await self._serve_thumbnail(_thumbnail_query(), out.getvalue())

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["x-thumbnail-reason"] == "reencoded"
        assert Image.open(BytesIO(response.content)).format == "JPEG"
        assert writes[0][3] == response.content

    @pytest.mark.asyncio
    async def test_thumbnail_over_the_decode_cap_is_a_502_and_not_cached(
        self, slide_settings, monkeypatch
    ):
        monkeypatch.setattr(main_module.settings, "thumbnail_max_decode_pixels", 1024 * 768 - 1)
        response, _, writes = await self._serve_thumbnail(
            _thumbnail_query(), _jpeg((1024, 768))
        )

        assert response.status_code == 502
        assert writes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lie",
        [
            # Claims the master already fits, which would cache it unresized.
            {"tw": "1", "th": "1"},
            # Claims a huge master.
            {"tw": "8192", "th": "8192"},
            # Malformed values are ignored too, not rejected.
            {"tw": "abc", "th": "-1"},
        ],
    )
    async def test_client_thumbnail_dimensions_cannot_change_what_is_cached(
        self, slide_settings, lie
    ):
        master = _jpeg((1024, 768))
        honest, _, honest_writes = await self._serve_thumbnail(_thumbnail_query(), master)
        lying, _, lying_writes = await self._serve_thumbnail(
            _thumbnail_query(**lie), master
        )

        assert honest.status_code == lying.status_code == 200
        assert lying.content == honest.content
        assert lying.headers["x-thumbnail-reason"] == "resized"
        assert lying_writes == honest_writes
        assert Image.open(BytesIO(lying_writes[0][3])).size == (128, 96)

    @pytest.mark.asyncio
    async def test_repeated_tw_th_parameters_are_ignored(self, slide_settings):
        params = list(_thumbnail_query().items()) + [("tw", "1"), ("tw", "2"), ("TH", "3")]
        response, _, _ = await self._serve_thumbnail(params, _jpeg((1024, 768)))

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unreadable_stored_thumbnail_is_unavailable(self, slide_settings):
        response, _, writes = await self._serve_thumbnail(_thumbnail_query(), b"not a jpeg")

        assert response.status_code == 502
        assert writes == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/tiles/zxy/0/0/0", "/thumbnails"])
    @pytest.mark.parametrize(
        "overrides",
        [
            {"studyId": None},
            {"studyId": ""},
            {"studyId": "study a"},
            {"studyId": "study/a"},
            {"studyId": "s" * 129},
            {"studyId": "."},
            {"studyId": ".."},
            {"studyId": "..."},
            {"key": None},
            {"key": ""},
            {"key": "a/b"},
            {"key": "a.b"},
            {"key": "k" * 129},
            {"enc": None},
            {"enc": ""},
            {"enc": "not+base64url"},
            {"enc": "A" * 4097},
        ],
    )
    async def test_missing_or_malformed_slide_parameters_are_rejected(
        self, slide_settings, path, overrides
    ):
        params = (_query if path.startswith("/tiles") else _thumbnail_query)(**overrides)
        with (
            patch.object(main_module.tile_cache, "get_tile", new=AsyncMock(return_value=b"x")),
            patch.object(main_module.tile_cache, "get_thumbnail", new=AsyncMock(return_value=b"x")),
        ):
            response = await _get(path, params)

        assert response.status_code == 400
        assert "s3://" not in response.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["studyId", "key", "enc"])
    @pytest.mark.parametrize("repeat_as", [str, str.upper, str.lower, str.title])
    async def test_repeated_slide_parameters_are_rejected_in_any_case(
        self, slide_settings, name, repeat_as
    ):
        # nginx `$arg_` lookups ignore case, so `StudyID=a&studyId=b` is a repeat.
        params = list(_query().items()) + [(repeat_as(name), _query()[name])]
        cached = AsyncMock(return_value=b"x")
        with patch.object(main_module.tile_cache, "get_tile", new=cached):
            response = await _get("/tiles/zxy/0/0/0", params)

        assert response.status_code == 400
        cached.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_parameter_names_match_case_insensitively(self, slide_settings):
        params = {"StudyID": STUDY_ID, "KEY": SLIDE_KEY, "Enc": make_enc()}
        cached = AsyncMock(return_value=b"tile")
        with patch.object(main_module.tile_cache, "get_tile", new=cached):
            response = await _get("/tiles/zxy/0/0/0", params)

        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "overrides",
        [
            {"width": ""},
            {"width": "0"},
            {"height": "-1"},
            {"width": "1.5"},
            {"height": "abc"},
            {"width": "01"},
            {"height": "1" * 10},
        ],
    )
    async def test_malformed_thumbnail_size_is_a_400(self, slide_settings, overrides):
        cached = AsyncMock(return_value=b"x")
        with patch.object(main_module.tile_cache, "get_thumbnail", new=cached):
            response = await _get("/thumbnails", _thumbnail_query(**overrides))

        assert response.status_code == 400
        cached.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["width", "height", "WIDTH"])
    async def test_repeated_thumbnail_size_is_a_400(self, slide_settings, name):
        params = list(_thumbnail_query().items()) + [(name, "64")]
        cached = AsyncMock(return_value=b"x")
        with patch.object(main_module.tile_cache, "get_thumbnail", new=cached):
            response = await _get("/thumbnails", params)

        assert response.status_code == 400
        cached.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("overrides", "size"),
        [
            ({"width": None, "height": None}, (256, 256)),
            ({"width": "64", "height": None}, (64, 256)),
            ({"width": "99999", "height": "3000"}, (2048, 2048)),
            ({"width": "1", "height": "1"}, (1, 1)),
        ],
    )
    async def test_thumbnail_size_is_optional_and_clamped(
        self, slide_settings, overrides, size
    ):
        cached = AsyncMock(return_value=b"thumb")
        with patch.object(main_module.tile_cache, "get_thumbnail", new=cached):
            response = await _get("/thumbnails", _thumbnail_query(**overrides))

        assert response.status_code == 200
        assert cached.await_args.args[1:] == size

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/tiles/zxy/0/0/0", "/thumbnails"])
    @pytest.mark.parametrize(
        "overrides",
        [
            # Another slide's sealed source presented under this key.
            {"enc": make_enc(slide_key=OTHER_SLIDE_KEY)},
            # This slide's sealed source presented under another key.
            {"key": OTHER_SLIDE_KEY},
            # Sealed with a different seal key.
            {"enc": make_enc(seal_key=bytes(32))},
            # Well-formed but not a sealed source.
            {"enc": "A" * 40},
        ],
    )
    async def test_enc_that_does_not_open_under_the_key_is_not_found(
        self, slide_settings, path, overrides
    ):
        params = (_query if path.startswith("/tiles") else _thumbnail_query)(**overrides)
        cached = AsyncMock(return_value=b"x")
        with (
            patch.object(main_module.tile_cache, "get_tile", new=cached),
            patch.object(main_module.tile_cache, "get_thumbnail", new=cached),
        ):
            response = await _get(path, params)

        assert response.status_code == 404
        assert response.json() == {"detail": "slide not found"}
        assert SLIDE_KEY not in response.text
        assert OTHER_SLIDE_KEY not in response.text
        cached.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_requests_fail_closed_without_a_seal_key(
        self, slide_settings, monkeypatch
    ):
        monkeypatch.setattr(main_module, "_source_seal_key", None)
        with patch.object(main_module.tile_cache, "get_tile", new=AsyncMock(return_value=b"x")):
            response = await _get("/tiles/zxy/0/0/0", _query())

        assert response.status_code == 503

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "sealed"),
        [
            # Outside the approved prefix.
            ("/tiles/zxy/0/0/0", {"tile_source": "s3://other/slide-a.svs"}),
            ("/thumbnails", {"thumbnail_source": "s3://other/slide-a.jpg"}),
            # Wrong artifact extension for the operation.
            ("/tiles/zxy/0/0/0", {"tile_source": "s3://slides/slide-a.jpg"}),
            ("/thumbnails", {"thumbnail_source": "s3://thumbs/slide-a.svs"}),
            # Query strings, traversal and embedded credentials.
            ("/tiles/zxy/0/0/0", {"tile_source": "s3://slides/slide-a.svs?versionId=1"}),
            ("/thumbnails", {"thumbnail_source": "s3://thumbs/../private/slide-a.jpg"}),
            ("/tiles/zxy/0/0/0", {"tile_source": "s3://user:pass@slides/slide-a.svs"}),
            # Unsupported schemes.
            ("/tiles/zxy/0/0/0", {"tile_source": "https://slides/slide-a.svs"}),
            ("/tiles/zxy/0/0/0", {"tile_source": "gs://slides/slide-a.svs"}),
        ],
    )
    async def test_sealed_source_outside_policy_is_not_found(
        self, slide_settings, path, sealed, caplog
    ):
        enc = make_enc(**sealed)
        params = (_query if path.startswith("/tiles") else _thumbnail_query)(enc=enc)
        cached = AsyncMock(return_value=b"x")
        with (
            patch.object(main_module.tile_cache, "get_tile", new=cached),
            patch.object(main_module.tile_cache, "get_thumbnail", new=cached),
        ):
            response = await _get(path, params)

        assert response.status_code == 404
        assert response.json() == {"detail": "source slide not found"}
        assert "outside the de-id policy" in caplog.text
        for text in (response.text, caplog.text):
            assert "s3://" not in text
            assert "slide-a" not in text
        cached.assert_not_awaited()

    @pytest.mark.parametrize("operation", ["tile", "thumbnail"])
    def test_source_outside_the_prefix_allowlist_is_not_found(
        self, slide_settings, operation
    ):
        claims = {"tile_source": "s3://other/slide-a.svs", "thumbnail_source": "s3://other/slide-a.jpg"}
        with pytest.raises(main_module.HTTPException) as exc_info:
            main_module._authorize_source(claims, operation)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/tiles/zxy/0/0/0", "/thumbnails"])
    @pytest.mark.parametrize(
        ("extra", "headers"),
        [
            ({"source": "s3://slides/slide-a.svs"}, {}),
            ({"source": ""}, {}),
            ({}, {"X-WSI-Source": "s3://slides/slide-a.svs"}),
            ({}, {"x-wsi-source": ""}),
            ({"Source": "s3://slides/slide-a.svs"}, {}),
        ],
    )
    async def test_routes_reject_client_supplied_sources(
        self, slide_settings, path, extra, headers
    ):
        params = (_query if path.startswith("/tiles") else _thumbnail_query)()
        params.update(extra)
        with (
            patch.object(main_module.tile_cache, "get_tile", new=AsyncMock(return_value=b"x")),
            patch.object(main_module.tile_cache, "get_thumbnail", new=AsyncMock(return_value=b"x")),
        ):
            response = await _get(path, params, headers)

        assert response.status_code == 400
        assert "s3://" not in response.text

    @pytest.mark.asyncio
    async def test_authorization_header_is_neither_required_nor_used(self, slide_settings):
        cached = AsyncMock(return_value=b"tile")
        with patch.object(main_module.tile_cache, "get_tile", new=cached):
            response = await _get(
                "/tiles/zxy/0/0/0", _query(), {"Authorization": "Bearer not-a-token"}
            )

        assert response.status_code == 200


class TestRateLimitScope:
    @staticmethod
    async def _scopes(headers_list) -> list[tuple]:
        distributed = AsyncMock(
            side_effect=lambda *args: (
                (b"jpeg", {"status": "ok", "reason": "test"})
                if args[1] == "thumbnail"
                else b"jpeg"
            )
        )
        transport = httpx.ASGITransport(app=main_module.app, client=("127.0.0.1", 5000))
        with (
            patch.object(main_module.tile_cache, "get_tile", new=AsyncMock(return_value=None)),
            patch.object(main_module.tile_cache, "get_thumbnail", new=AsyncMock(return_value=None)),
            patch.object(main_module, "_distributed_singleflight", new=distributed),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for headers in headers_list:
                    tile_response = await client.get(
                        "/tiles/zxy/0/0/0", params=_query(), headers=headers
                    )
                    thumbnail_response = await client.get(
                        "/thumbnails", params=_thumbnail_query(), headers=headers
                    )
                    assert tile_response.status_code == 200
                    assert thumbnail_response.status_code == 200
        return [call.args[2:4] for call in distributed.await_args_list]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("headers", "subject"),
        [
            ({}, "127.0.0.1"),
            ({"X-Forwarded-For": "10.1.2.3"}, "10.1.2.3"),
            ({"X-Forwarded-For": " 10.1.2.3 , 172.16.0.1 "}, "172.16.0.1"),
            ({"X-Forwarded-For": "spoofed, 10.9.9.9, 172.16.0.1"}, "172.16.0.1"),
            ({"X-Forwarded-For": ""}, "127.0.0.1"),
            ({"X-Forwarded-For": "10.1.2.3, "}, "127.0.0.1"),
        ],
    )
    async def test_tile_and_thumbnail_scope_by_ingress_client_and_key(
        self, slide_settings, headers, subject
    ):
        assert await self._scopes([headers]) == [(subject, SLIDE_KEY), (subject, SLIDE_KEY)]

    @pytest.mark.asyncio
    async def test_rotating_the_leftmost_hop_does_not_create_new_buckets(
        self, slide_settings
    ):
        scopes = await self._scopes(
            [{"X-Forwarded-For": f"10.0.0.{n}, 172.16.0.1"} for n in range(5)]
        )

        assert len(scopes) == 10
        assert set(scopes) == {("172.16.0.1", SLIDE_KEY)}

    @pytest.mark.asyncio
    async def test_study_id_does_not_change_the_scope(self, slide_settings):
        distributed = AsyncMock(return_value=b"jpeg")
        with (
            patch.object(main_module.tile_cache, "get_tile", new=AsyncMock(return_value=None)),
            patch.object(main_module, "_distributed_singleflight", new=distributed),
        ):
            for study_id in ("study_a", "study_b", "study_c"):
                response = await _get("/tiles/zxy/0/0/0", _query(studyId=study_id))
                assert response.status_code == 200

        assert {call.args[3] for call in distributed.await_args_list} == {SLIDE_KEY}
