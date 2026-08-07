"""Tests for FastAPI HTTP routes (app/main.py)."""

import base64
import hashlib
import hmac
import json
import logging
import time
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.cache as cache_module
import app.main as main_module
import app.meta as meta_module
from app.config import settings
from app.rate_limit import RequestRateLimiter
from app.resource_index import ResourceIndex
from app.thumbnail_store import ThumbnailRecord
from app.tiles import TILE_SIZE
from tests.test_auth import make_token
from tests.conftest import make_mock_slide


def make_wsi_token(secret: str, study_id: str, **overrides) -> str:
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    now = int(time.time())
    claims = {
        "sub": "wsi-test-user",
        "aud": "cbioportal-wsi",
        "scope": "wsi:read",
        "study_id": study_id,
        "wsi_auth_version": 1,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    header = encode({"alg": "HS256", "typ": "JWT"})
    payload = encode(claims)
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def configure_resource_auth(monkeypatch, tmp_path):
    secret = "wsi-test-secret-0123456789abcdef"
    resource_index = tmp_path / "wsi-resource-index.json"
    resource_index.write_text(
        json.dumps(
            {
                "version": 2,
                "studies": {
                    "study-a": {
                        "patients": ["P-a"],
                        "samples": ["sample-a"],
                        "slides": {
                            "1492807": {
                                "patient_id": "P-a",
                                "source_path": "s3://test-bucket/1492807.svs",
                            }
                        },
                    },
                    "study-b": {
                        "patients": ["P-b"],
                        "samples": ["sample-b"],
                        "slides": {
                            "2492807": {
                                "patient_id": "P-b",
                                "source_path": "s3://test-bucket/2492807.svs",
                            }
                        },
                    },
                },
            }
        )
    )
    monkeypatch.setattr(settings, "wsi_auth_required", True)
    monkeypatch.setattr(settings, "wsi_auth_secret", secret)
    monkeypatch.setattr(settings, "wsi_auth_audience", "cbioportal-wsi")
    monkeypatch.setattr(settings, "wsi_auth_max_ttl", 900)
    monkeypatch.setattr(settings, "wsi_resource_index_file", str(resource_index))
    return secret


@pytest.fixture(autouse=False)
def api_client():
    """
    TestClient with all external deps mocked:
    - Redis cache: all get/set → no-ops
    - meta.get_slide_path: returns a fake S3 URI for any image_id
    - SlideCache: returns a mock slide
    - init_cache / close_cache: no-ops (avoids Redis connection)
    """
    mock_slide = make_mock_slide()

    # Mock thumbnail (TiffSlide.get_thumbnail is not on our basic mock)
    mock_slide.get_thumbnail = MagicMock(
        return_value=Image.new("RGBA", (256, 256), (200, 200, 200, 255))
    )

    async def _noop_get(*a, **k):
        return None

    async def _noop_set(*a, **k):
        pass

    async def _noop_init():
        pass

    patches = [
        patch.object(cache_module, "init_cache",    _noop_init),
        patch.object(cache_module, "close_cache",   _noop_init),
        patch.object(cache_module, "get_tile",      _noop_get),
        patch.object(cache_module, "set_tile",      _noop_set),
        patch.object(cache_module, "get_thumbnail", _noop_get),
        patch.object(cache_module, "get_thumbnail_status", _noop_get),
        patch.object(cache_module, "set_thumbnail", _noop_set),
        patch.object(cache_module, "set_thumbnail_status", _noop_set),
        patch.object(cache_module, "get_metadata",  _noop_get),
        patch.object(cache_module, "set_metadata",  _noop_set),
        patch.object(cache_module, "get_raw",       _noop_get),
        patch.object(cache_module, "set_raw",       _noop_set),
        patch.object(meta_module, "get_slide_path",
                     lambda image_id, warehouse_id: f"s3://test-bucket/{image_id}.svs"),
    ]
    for p in patches:
        p.start()

    with TestClient(main_module.app) as client:
        # Lifespan has run by now; replace _slides and clear path cache
        main_module._slides = MagicMock()
        main_module._slides.get          = MagicMock(return_value=mock_slide)
        main_module._slides.close_all    = MagicMock()
        main_module._path_cache.clear()
        yield client

    main_module._path_cache.clear()
    for p in patches:
        p.stop()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_status_ok(self, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_n_workers_present(self, api_client):
        data = api_client.get("/health").json()
        assert "n_workers" in data
        assert isinstance(data["n_workers"], int)
        assert data["n_workers"] > 0

    def test_thumbnail_generation_is_degraded_without_manifest(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "thumbnail_manifest_uri", "")
        data = api_client.get("/health").json()
        assert data["thumbnail_generation"] == {
            "status": "degraded",
            "reason": "thumbnail_manifest_uri_missing",
        }

    def test_thumbnail_generation_is_ready_with_manifest(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "thumbnail_manifest_uri", "s3://bucket/manifest.json")
        data = api_client.get("/health").json()
        assert data["thumbnail_generation"] == {"status": "ready"}

    def test_wsi_namespace_health(self, api_client):
        resp = api_client.get("/wsi/health")
        assert resp.status_code == 200

    def test_ready_is_ok_without_auth(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "wsi_auth_required", False)
        resp = api_client.get("/ready")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["auth_required"] is False

    def test_wsi_namespace_ready(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "wsi_auth_required", False)
        resp = api_client.get("/wsi/ready")
        assert resp.status_code == 200

    def test_ready_reports_missing_resource_index_when_auth_required(
        self, api_client, monkeypatch
    ):
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_resource_index_file", "")
        resp = api_client.get("/ready")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unavailable"
        assert "resource index" in resp.json()["reason"]

    def test_ready_is_ok_with_valid_resource_index(
        self, api_client, monkeypatch, tmp_path
    ):
        configure_resource_auth(monkeypatch, tmp_path)
        expected_revision = ResourceIndex(settings.wsi_resource_index_file).revision()

        resp = api_client.get("/ready")
        alias_resp = api_client.get("/wsi/ready")

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        assert resp.json()["auth_required"] is True
        assert resp.json()["resource_index_revision"] == expected_revision
        assert alias_resp.status_code == 200
        assert alias_resp.json()["resource_index_revision"] == expected_revision

    def test_wsi_data_requires_capability(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_auth_secret", "s" * 32)
        resp = api_client.get("/wsi/tiles/1/metadata")
        assert resp.status_code == 401

    def test_removed_patient_fallback_is_not_registered(self, api_client, monkeypatch, tmp_path):
        assert api_client.get("/internal/patient/P-1").status_code == 404
        assert api_client.get("/wsi/internal/patient/P-1").status_code == 404

        secret = configure_resource_auth(monkeypatch, tmp_path)
        token = make_wsi_token(secret, "study-a")
        headers = {"Authorization": f"Bearer {token}"}
        assert api_client.get("/internal/patient/P-1", headers=headers).status_code == 404
        assert api_client.get("/wsi/internal/patient/P-1", headers=headers).status_code == 404

    def test_health_is_exempt_from_rate_limit(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
        monkeypatch.setattr(main_module, "rate_limiter", RequestRateLimiter())
        assert api_client.get("/health").status_code == 200
        assert api_client.get("/health").status_code == 200
        assert api_client.get("/ready").status_code in (200, 503)
        assert api_client.get("/ready").status_code in (200, 503)

    def test_unauthenticated_expensive_requests_do_not_consume_quota(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_auth_secret", "s" * 32)
        monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
        monkeypatch.setattr(main_module, "rate_limiter", RequestRateLimiter())
        assert api_client.get("/search?q=P-1").status_code == 401
        limited = api_client.get("/search?q=P-2")
        assert limited.status_code == 401

    def test_expensive_requests_are_rate_limited_per_subject(self, api_client, monkeypatch, tmp_path):
        secret = configure_resource_auth(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
        monkeypatch.setattr(main_module, "rate_limiter", RequestRateLimiter())

        now = int(time.time())
        alice = make_token(
            secret,
            sub="alice@example.org",
            aud=settings.wsi_auth_audience,
            scope="wsi:read",
            study_id="study-a",
            wsi_auth_version=1,
            iat=now,
            exp=now + settings.wsi_auth_max_ttl,
        )
        bob = make_token(
            secret,
            sub="bob@example.org",
            aud=settings.wsi_auth_audience,
            scope="wsi:read",
            study_id="study-a",
            wsi_auth_version=1,
            iat=now,
            exp=now + settings.wsi_auth_max_ttl,
        )

        with patch.object(main_module, "search_suggestions", return_value=[]):
            allowed = api_client.get(
                "/search?q=P-1", headers={"Authorization": f"Bearer {alice}"}
            )
            limited = api_client.get(
                "/search?q=P-2", headers={"Authorization": f"Bearer {alice}"}
            )
            other_subject = api_client.get(
                "/search?q=P-3", headers={"Authorization": f"Bearer {bob}"}
            )

        assert allowed.status_code == 200
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        assert other_subject.status_code == 200

    def test_thumbnail_requests_are_rate_limited_per_subject(
        self, api_client, monkeypatch, tmp_path
    ):
        secret = configure_resource_auth(monkeypatch, tmp_path)
        monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
        monkeypatch.setattr(main_module, "rate_limiter", RequestRateLimiter())
        token = make_wsi_token(secret, "study-a")
        headers = {"Authorization": f"Bearer {token}"}

        with (
            patch.object(main_module, "get_thumbnail_record", return_value=None),
            patch.object(main_module, "get_persisted_generated_thumbnail_record", return_value=None),
            patch.object(main_module, "_generate_thumbnail_record_on_demand", new=AsyncMock(return_value=None)),
        ):
            assert api_client.get("/thumbnails/1492807", headers=headers).status_code == 200
            limited = api_client.get("/thumbnails/1492807", headers=headers)
            other_subject = api_client.get(
                "/thumbnails/1492807",
                headers={
                    "Authorization": f"Bearer {make_wsi_token(secret, 'study-a', sub='other-user')}"
                },
            )

        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        assert other_subject.status_code == 200

    def test_study_capability_binds_slide_resources(self, api_client, monkeypatch, tmp_path):
        secret = configure_resource_auth(monkeypatch, tmp_path)
        token_a = make_wsi_token(secret, "study-a")

        allowed = api_client.get(
            "/tiles/1492807/metadata", headers={"Authorization": f"Bearer {token_a}"}
        )
        denied = api_client.get(
            "/tiles/2492807/metadata", headers={"Authorization": f"Bearer {token_a}"}
        )
        mismatched_query = api_client.get(
            "/tiles/1492807/metadata?studyId=study-b",
            headers={"Authorization": f"Bearer {token_a}"},
        )

        assert allowed.status_code == 200
        assert denied.status_code == 403
        assert mismatched_query.status_code == 403
        assert "private" in allowed.headers["cache-control"]
        assert "public" not in allowed.headers["cache-control"]

    def test_indexed_slide_without_source_returns_not_found(self, api_client, monkeypatch, tmp_path):
        secret = configure_resource_auth(monkeypatch, tmp_path)
        resource_index = json.loads(
            (tmp_path / "wsi-resource-index.json").read_text()
        )
        resource_index["studies"]["study-a"]["slides"]["1492807"]["source_path"] = None
        (tmp_path / "wsi-resource-index.json").write_text(json.dumps(resource_index))

        response = api_client.get(
            "/tiles/1492807/metadata",
            headers={"Authorization": f"Bearer {make_wsi_token(secret, 'study-a')}"},
        )

        assert response.status_code == 404

    def test_missing_resource_index_fails_closed(self, api_client, monkeypatch):
        secret = "wsi-test-secret-0123456789abcdef"
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_auth_secret", secret)
        monkeypatch.setattr(settings, "wsi_auth_audience", "cbioportal-wsi")
        monkeypatch.setattr(settings, "wsi_auth_max_ttl", 900)
        monkeypatch.setattr(settings, "wsi_resource_index_file", "")

        response = api_client.get(
            "/tiles/1492807/metadata",
            headers={"Authorization": f"Bearer {make_wsi_token(secret, 'study-a')}"},
        )

        assert response.status_code == 503

    def test_version_one_resource_index_fails_closed(self, api_client, monkeypatch, tmp_path):
        secret = "wsi-test-secret-0123456789abcdef"
        resource_index = tmp_path / "legacy-resource-index.json"
        resource_index.write_text(
            json.dumps({"version": 1, "studies": {"study-a": {"slides": ["1492807"]}}})
        )
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_auth_secret", secret)
        monkeypatch.setattr(settings, "wsi_auth_audience", "cbioportal-wsi")
        monkeypatch.setattr(settings, "wsi_auth_max_ttl", 900)
        monkeypatch.setattr(settings, "wsi_resource_index_file", str(resource_index))

        response = api_client.get(
            "/tiles/1492807/metadata",
            headers={"Authorization": f"Bearer {make_wsi_token(secret, 'study-a')}"},
        )

        assert response.status_code == 503

    def test_cross_study_duplicate_resource_ids_are_scoped(self, api_client, monkeypatch, tmp_path):
        secret = "wsi-test-secret-0123456789abcdef"
        resource_index = tmp_path / "ambiguous-resource-index.json"
        resource_index.write_text(
            json.dumps(
                {
                    "version": 2,
                    "studies": {
                        "study-a": {"patients": ["P-same"], "samples": [], "slides": {}},
                        "study-b": {"patients": ["P-same"], "samples": [], "slides": {}},
                    },
                }
            )
        )
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_auth_secret", secret)
        monkeypatch.setattr(settings, "wsi_auth_audience", "cbioportal-wsi")
        monkeypatch.setattr(settings, "wsi_auth_max_ttl", 900)
        monkeypatch.setattr(settings, "wsi_resource_index_file", str(resource_index))

        response = api_client.get(
            "/search?q=P-same",
            headers={"Authorization": f"Bearer {make_wsi_token(secret, 'study-a')}"},
        )

        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == ["P-same"]
        index = ResourceIndex(str(resource_index))
        assert index.contains("study-a", "patients", "P-same")
        assert index.contains("study-b", "patients", "P-same")

    def test_duplicate_slide_id_uses_the_token_study_binding(self, api_client, monkeypatch, tmp_path):
        secret = "wsi-test-secret-0123456789abcdef"
        resource_index = tmp_path / "resource-index.json"
        resource_index.write_text(
            json.dumps(
                {
                    "version": 2,
                    "studies": {
                        "study-a": {
                            "patients": ["P-a"],
                            "samples": [],
                            "slides": {
                                "same-slide": {
                                    "patient_id": "P-a",
                                    "source_path": "s3://test-bucket/study-a.svs",
                                }
                            },
                        },
                        "study-b": {
                            "patients": ["P-b"],
                            "samples": [],
                            "slides": {
                                "same-slide": {
                                    "patient_id": "P-b",
                                    "source_path": "s3://test-bucket/study-b.svs",
                                }
                            },
                        },
                    },
                }
            )
        )
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_auth_secret", secret)
        monkeypatch.setattr(settings, "wsi_auth_audience", "cbioportal-wsi")
        monkeypatch.setattr(settings, "wsi_auth_max_ttl", 900)
        monkeypatch.setattr(settings, "wsi_resource_index_file", str(resource_index))

        response_a = api_client.get(
            "/tiles/same-slide/metadata",
            headers={"Authorization": f"Bearer {make_wsi_token(secret, 'study-a')}"},
        )
        response_b = api_client.get(
            "/tiles/same-slide/metadata",
            headers={"Authorization": f"Bearer {make_wsi_token(secret, 'study-b')}"},
        )

        assert response_a.status_code == 200
        assert response_b.status_code == 200
        assert main_module._slides.get.call_args_list[-2].args == ("s3://test-bucket/study-a.svs",)
        assert main_module._slides.get.call_args_list[-1].args == ("s3://test-bucket/study-b.svs",)

    def test_raw_metadata_and_search_are_study_filtered(
        self, api_client, monkeypatch, tmp_path
    ):
        secret = configure_resource_auth(monkeypatch, tmp_path)
        token_a = make_wsi_token(secret, "study-a")
        metadata_args = []

        async def fake_in_thread(fn, *args):
            if fn is main_module.get_slide_dbmeta:
                metadata_args.extend(args)
                return {
                    "image_id": args[0],
                    "stain_name": None,
                    "stain_group": None,
                    "magnification": None,
                    "file_size_bytes": None,
                }
            return fn(*args)

        with patch.object(main_module, "_in_thread", new=fake_in_thread):
            raw_metadata = api_client.get(
                "/slides/1492807/dbmeta", headers={"Authorization": f"Bearer {token_a}"}
            )
            search = api_client.get(
                "/search?q=P-a", headers={"Authorization": f"Bearer {token_a}"}
            )

        assert raw_metadata.status_code == 200
        assert set(raw_metadata.json()) == {
            "image_id",
            "stain_name",
            "stain_group",
            "magnification",
            "file_size_bytes",
        }
        assert metadata_args[2] == "P-a"
        assert [item["id"] for item in search.json()] == ["P-a"]

        forbidden = api_client.get(
            "/slides/2492807/dbmeta",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        assert forbidden.status_code == 403

    def test_raw_metadata_requires_capability(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "wsi_auth_required", True)
        monkeypatch.setattr(settings, "wsi_auth_secret", "s" * 32)
        response = api_client.get("/slides/1492807/dbmeta")
        assert response.status_code == 401

    def test_authenticated_search_checks_cache_before_building_suggestions(
        self, api_client, monkeypatch, tmp_path
    ):
        secret = configure_resource_auth(monkeypatch, tmp_path)
        token_a = make_wsi_token(secret, "study-a")
        cached = [{"type": "patient", "id": "P-a", "label": "P-a", "sublabel": ""}]

        with patch.object(cache_module, "get_raw", new=AsyncMock(return_value=cached)) as get_raw:
            with patch.object(ResourceIndex, "suggestions", side_effect=AssertionError("cache miss")):
                response = api_client.get(
                    "/search?q=P-a", headers={"Authorization": f"Bearer {token_a}"}
                )

        assert response.status_code == 200
        assert response.json() == cached
        assert get_raw.await_count == 1
        assert get_raw.await_args.args[0].startswith("search:study-a:")


# ---------------------------------------------------------------------------
# /tiles/{slide_id}/metadata
# ---------------------------------------------------------------------------

class TestMetadataRoute:
    def test_test_slide_map_bypasses_databricks_lookup(self, api_client, monkeypatch):
        monkeypatch.setattr(settings, "test_slide_map", {"tiny-slide": "/app/testdata/CMU-1-Small-Region.svs"})
        with patch.object(meta_module, "get_slide_path", side_effect=AssertionError("should not query databricks")):
            resp = api_client.get("/tiles/tiny-slide/metadata")
        assert resp.status_code == 200

    def test_returns_200_with_shape(self, api_client):
        resp = api_client.get("/tiles/1492807/metadata")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("dimensions", "mpp", "vendor", "objective_power",
                    "max_zoom", "tile_size", "levels"):
            assert key in data, f"missing key: {key}"

    def test_mpp_values(self, api_client):
        data = api_client.get("/tiles/1492807/metadata").json()
        assert data["mpp"]["x"] == pytest.approx(0.5034)
        assert data["mpp"]["y"] == pytest.approx(0.5034)

    def test_objective_power(self, api_client):
        data = api_client.get("/tiles/1492807/metadata").json()
        assert data["objective_power"] == 20

    def test_missing_slide_returns_4xx(self, api_client):
        main_module._slides.get.side_effect = FileNotFoundError("gone")
        try:
            resp = api_client.get("/tiles/missing/metadata")
            assert resp.status_code in (404, 500)
        finally:
            main_module._slides.get.side_effect = None

    def test_warmup_resolves_image_id_before_opening(self, api_client):
        with patch.object(
            meta_module,
            "get_slide_path",
            return_value="s3://test-bucket/1492807.svs",
        ) as get_path:
            resp = api_client.get("/tiles/1492807/warmup")

        assert resp.status_code == 200
        get_path.assert_called_once_with("1492807", settings.databricks_warehouse_id)
        main_module._slides.get.assert_called_with("s3://test-bucket/1492807.svs")


# ---------------------------------------------------------------------------
# /tiles/{slide_id}/zxy/{z}/{x}/{y}
# ---------------------------------------------------------------------------

class TestTileRoute:
    def test_valid_tile_returns_jpeg(self, api_client):
        resp = api_client.get("/tiles/1492807/zxy/0/0/0")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content[:2] == b"\xff\xd8"

    def test_cache_control_is_private(self, api_client):
        resp = api_client.get("/tiles/1492807/zxy/0/0/0")
        cache_control = resp.headers.get("cache-control", "")
        assert "private" in cache_control
        assert "public" not in cache_control

    def test_warmup_cache_control_is_private(self, api_client):
        resp = api_client.get("/tiles/1492807/warmup")
        cache_control = resp.headers.get("cache-control", "")
        assert "private" in cache_control
        assert "public" not in cache_control

    def test_out_of_range_z_returns_404(self, api_client):
        resp = api_client.get("/tiles/1492807/zxy/99/0/0")
        assert resp.status_code == 404

    def test_out_of_bounds_xy_returns_404(self, api_client):
        # mock slide is 1024×1024, max_zoom=2; x=999 is way out
        resp = api_client.get("/tiles/1492807/zxy/2/999/0")
        assert resp.status_code == 404

    def test_oversized_overview_returns_422(self, api_client):
        main_module._slides.get.return_value = make_mock_slide(4096, 4096, levels=1)
        resp = api_client.get("/tiles/1492807/zxy/0/0/0")
        assert resp.status_code == 422
        assert resp.json()["detail"]["error"] == "overview_requires_preprocessing"


# ---------------------------------------------------------------------------
# /thumbnails/{slide_id}
# ---------------------------------------------------------------------------

class TestThumbnailRoute:
    def test_thumbnail_logs_do_not_include_slide_id(self, api_client, caplog):
        record = ThumbnailRecord(
            image_id="1492807",
            uri="s3://thumbs/1492807.jpg",
            width=1024,
            height=768,
        )
        caplog.set_level(logging.INFO, logger=main_module.__name__)
        with (
            patch.object(main_module, "get_thumbnail_record", return_value=record),
            patch.object(
                main_module,
                "render_thumbnail_response",
                return_value=(b"\xff\xd8thumb", {"status": "ok", "reason": "resized"}),
            ),
        ):
            response = api_client.get("/thumbnails/1492807?width=256&height=256")

        assert response.status_code == 200
        assert "1492807" not in caplog.text

    def test_returns_jpeg(self, api_client):
        record = ThumbnailRecord(
            image_id="1492807",
            uri="s3://thumbs/1492807.jpg",
            width=1024,
            height=768,
        )
        with (
            patch.object(main_module, "get_thumbnail_record", return_value=record),
            patch.object(
                main_module,
                "render_thumbnail_response",
                return_value=(b"\xff\xd8thumb", {"status": "ok", "reason": "resized"}),
            ),
        ):
            resp = api_client.get("/thumbnails/1492807?width=256&height=256")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content[:2] == b"\xff\xd8"
        assert resp.headers["x-thumbnail-status"] == "ok"

    def test_width_clamped_to_max(self, api_client):
        record = ThumbnailRecord(
            image_id="1492807",
            uri="s3://thumbs/1492807.jpg",
            width=1024,
            height=768,
        )
        with (
            patch.object(main_module, "get_thumbnail_record", return_value=record),
            patch.object(
                main_module,
                "render_thumbnail_response",
                return_value=(b"\xff\xd8thumb", {"status": "ok", "reason": "master"}),
            ) as render,
        ):
            resp = api_client.get("/thumbnails/1492807?width=9999&height=256")
        assert resp.status_code == 200
        render.assert_called_once_with(record, 2048, 256)

    def test_cache_control_present(self, api_client):
        resp = api_client.get("/thumbnails/1492807")
        assert "max-age" in resp.headers.get("cache-control", "")

    def test_placeholder_cache_control_is_short_lived(self, api_client):
        resp = api_client.get("/thumbnails/1492807")
        assert resp.headers["x-thumbnail-status"] == "placeholder"
        assert resp.headers["cache-control"] == "private, max-age=60"

    def test_thumbnail_status_headers_are_exposed_to_allowed_origins(self, api_client):
        resp = api_client.get(
            "/thumbnails/1492807",
            headers={"Origin": "https://cbioportal.mskcc.org"},
        )
        assert "X-Thumbnail-Status" in resp.headers["access-control-expose-headers"]
        assert "X-Thumbnail-Reason" in resp.headers["access-control-expose-headers"]

    def test_missing_artifact_generates_on_demand(self, api_client):
        record = ThumbnailRecord(
            image_id="1492807",
            uri="s3://thumbs/1492807.jpg",
            width=1024,
            height=768,
        )
        with (
            patch.object(main_module, "get_thumbnail_record", return_value=None),
            patch.object(main_module, "get_persisted_generated_thumbnail_record", return_value=None),
            patch.object(main_module, "_generate_thumbnail_record_on_demand", new=AsyncMock(return_value=record)),
            patch.object(
                main_module,
                "render_thumbnail_response",
                return_value=(b"\xff\xd8thumb", {"status": "ok", "reason": "master"}),
            ),
        ):
            resp = api_client.get("/thumbnails/1492807?width=256&height=256")
        assert resp.status_code == 200
        assert resp.headers["x-thumbnail-status"] == "ok"

    def test_stale_manifest_artifact_generates_replacement(self, api_client):
        manifest_record = ThumbnailRecord(
            image_id="1492807",
            uri="s3://thumbs/old/1492807.jpg",
            width=1024,
            height=768,
        )
        generated_record = ThumbnailRecord(
            image_id="1492807",
            uri="s3://thumbs/masters/1492807.jpg",
            width=1024,
            height=768,
        )
        with (
            patch.object(main_module, "get_thumbnail_record", return_value=manifest_record),
            patch.object(main_module, "render_thumbnail_response", side_effect=[
                FileNotFoundError("old artifact"),
                (b"\xff\xd8thumb", {"status": "ok", "reason": "master"}),
            ]),
            patch.object(
                main_module,
                "_generate_thumbnail_record_on_demand",
                new=AsyncMock(return_value=generated_record),
            ) as generate,
        ):
            resp = api_client.get("/thumbnails/1492807?width=256&height=256")

        assert resp.status_code == 200
        generate.assert_awaited_once_with("1492807", None)
        assert resp.headers["x-thumbnail-status"] == "ok"

    def test_legacy_thumbnail_route_removed(self, api_client):
        resp = api_client.get("/tiles/1492807/thumbnail?width=256&height=256")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

class TestSearchRoute:
    def test_short_query_returns_empty(self, api_client):
        resp = api_client.get("/search?q=P")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_valid_query_returns_list(self, api_client):
        suggestions = [{"type": "patient", "id": "P-0001", "label": "P-0001", "sublabel": "CRC"}]
        with patch("app.main._in_thread", new=AsyncMock(return_value=suggestions)):
            resp = api_client.get("/search?q=P-0001")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_search_error_returns_502(self, api_client):
        async def _raise(*a, **k):
            raise RuntimeError("Search failed for P-SECRET at /private/path")

        with patch("app.main._in_thread", new=_raise):
            resp = api_client.get("/search?q=P-1234")
        assert resp.status_code == 502

    def test_search_error_log_excludes_query_and_exception_text(self, api_client, caplog):
        async def _raise(*a, **k):
            raise RuntimeError("Search failed for P-SECRET at /private/path")

        caplog.set_level(logging.INFO, logger=main_module.__name__)
        with patch("app.main._in_thread", new=_raise):
            resp = api_client.get("/search?q=P-SECRET")

        assert resp.status_code == 502
        assert "P-SECRET" not in caplog.text
        assert "/private/path" not in caplog.text
