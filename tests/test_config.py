"""Tests for app/config.py — verifies correct env var names are read.

These tests exist specifically to catch regressions like the
ECS_ENDPOINT_URL vs AWS_ENDPOINT_URL mismatch that silently broke
all S3 tile requests in production.
"""

import os
from unittest.mock import patch

import pytest

from app.config import Settings


def make_settings(**env):
    """Create a fresh Settings() with only the given env vars active."""
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("AWS_", "DATABRICKS_", "REDIS", "TILE_",
                                   "JPEG_", "MAX_", "N_WORKERS", "BLOCKCACHE",
                                   "THUMBNAIL_", "METADATA_", "PATH_",
                                   "CORS_", "CACHE_", "PATIENT_", "WSI_",
                                   "IMAGE_", "SLIDE_S3_", "ANNOTATION_",
                                   "KEYCLOAK_"))}
    clean.update(env)
    with patch.dict(os.environ, clean, clear=True):
        with patch("app.config._aws_profile", return_value=""):
            return Settings()


class TestS3EnvVars:
    def test_endpoint_reads_AWS_ENDPOINT_URL(self):
        s = make_settings(AWS_ENDPOINT_URL="http://ecs.example.com:9020")
        assert s.aws_endpoint_url == "http://ecs.example.com:9020"

    def test_endpoint_not_read_from_ECS_ENDPOINT_URL(self):
        """Regression: old name ECS_ENDPOINT_URL must NOT be used."""
        s = make_settings(ECS_ENDPOINT_URL="http://ecs.example.com:9020")
        assert s.aws_endpoint_url == ""

    def test_access_key_reads_AWS_ACCESS_KEY_ID(self):
        s = make_settings(AWS_ACCESS_KEY_ID="AKIATEST")
        assert s.aws_access_key_id == "AKIATEST"

    def test_secret_reads_AWS_SECRET_ACCESS_KEY(self):
        s = make_settings(AWS_SECRET_ACCESS_KEY="supersecret")
        assert s.aws_secret_access_key == "supersecret"

    def test_endpoint_empty_when_unset(self):
        s = make_settings()
        assert s.aws_endpoint_url == ""

    def test_env_takes_priority_over_profile(self):
        with patch("app.config._aws_profile", return_value="http://from-profile:9020"):
            with patch.dict(os.environ, {"AWS_ENDPOINT_URL": "http://from-env:9020"}):
                s = Settings()
        assert s.aws_endpoint_url == "http://from-env:9020"

    def test_profile_used_when_env_absent(self):
        clean = {k: v for k, v in os.environ.items() if k != "AWS_ENDPOINT_URL"}
        with patch.dict(os.environ, clean, clear=True):
            with patch("app.config._aws_profile", return_value="http://from-profile:9020"):
                s = Settings()
        assert s.aws_endpoint_url == "http://from-profile:9020"


class TestOtherSettings:
    def test_max_decode_pixels_default(self):
        s = make_settings()
        assert s.max_decode_pixels == 16_777_216

    def test_thumbnail_max_decode_pixels_default(self):
        s = make_settings()
        assert s.thumbnail_max_decode_pixels == 16_777_216

    def test_thumbnail_cache_ttl_default(self):
        s = make_settings()
        assert s.thumbnail_cache_ttl == 86_400

    def test_thumbnail_fetch_retry_defaults(self):
        s = make_settings()
        assert s.thumbnail_fetch_max_attempts == 2
        assert s.thumbnail_fetch_retry_delay_sec == 0.1

    def test_wsi_auth_max_ttl_reads_environment(self):
        s = make_settings(WSI_AUTH_MAX_TTL="600")
        assert s.wsi_auth_max_ttl == 600

    def test_max_image_operations_default(self):
        s = make_settings()
        assert s.max_image_operations == 2

    def test_cold_read_operation_guardrails_default(self):
        s = make_settings()
        assert s.image_operation_queue_timeout_seconds == 2.0
        assert s.slide_s3_connect_timeout_seconds == 1.0
        assert s.slide_s3_read_timeout_seconds == 10.0
        assert s.slide_s3_max_attempts == 2
        assert s.slide_s3_max_connections == 16

    def test_cold_read_timeout_guardrails_default(self):
        s = make_settings()
        assert s.cache_miss_lock_ttl_seconds == 120
        assert s.cache_miss_wait_timeout_seconds == 60.0

    def test_cache_miss_rate_limit_prefers_new_environment_name(self):
        s = make_settings(
            RATE_LIMIT_PER_MINUTE="7",
            CACHE_MISS_RATE_LIMIT_PER_MINUTE="120",
        )
        assert s.cache_miss_rate_limit_per_minute == 120

    def test_cache_miss_rate_limit_accepts_legacy_alias(self):
        s = make_settings(RATE_LIMIT_PER_MINUTE="7")
        assert s.cache_miss_rate_limit_per_minute == 7

    def test_test_slide_map_defaults_empty(self):
        s = make_settings()
        assert s.test_slide_map == {}

    def test_test_slide_map_reads_json_file(self, tmp_path):
        map_file = tmp_path / "slide-map.json"
        map_file.write_text('{"slide-a":"/app/testdata/slide-a.svs"}')
        s = make_settings(WSI_TEST_SLIDE_MAP_FILE=str(map_file))
        assert s.test_slide_map == {"slide-a": "/app/testdata/slide-a.svs"}

    def test_databricks_warehouse_id_from_env(self):
        s = make_settings(DATABRICKS_WAREHOUSE_ID="wh-test-123")
        assert s.databricks_warehouse_id == "wh-test-123"

    def test_use_canonical_association_table_defaults_true(self):
        s = make_settings()
        assert s.use_canonical_association_table is True

    def test_use_canonical_association_table_can_be_disabled(self):
        s = make_settings(USE_CANONICAL_ASSOCIATION_TABLE="false")
        assert s.use_canonical_association_table is False

    def test_allow_legacy_association_fallback_defaults_false(self):
        s = make_settings()
        assert s.allow_legacy_association_fallback is False

    def test_allow_legacy_association_fallback_can_be_disabled(self):
        s = make_settings(ALLOW_LEGACY_ASSOCIATION_FALLBACK="false")
        assert s.allow_legacy_association_fallback is False

    def test_allow_legacy_association_fallback_can_be_enabled(self):
        s = make_settings(ALLOW_LEGACY_ASSOCIATION_FALLBACK="true")
        assert s.allow_legacy_association_fallback is True

    def test_annotation_database_url_from_env(self):
        s = make_settings(ANNOTATION_DATABASE_URL="postgresql://user:pass@host/db")
        assert s.annotation_database_url == "postgresql://user:pass@host/db"

    def test_cors_origins_parsed(self):
        s = make_settings(CORS_ORIGINS="https://a.example.com,https://b.example.com")
        assert s.cors_origins == ["https://a.example.com", "https://b.example.com"]

    def test_cors_origins_strips_whitespace(self):
        s = make_settings(CORS_ORIGINS="https://a.example.com, https://b.example.com")
        assert s.cors_origins == ["https://a.example.com", "https://b.example.com"]

    def test_cors_origins_default_to_internal_cbioportal_hosts(self):
        s = make_settings()
        assert s.cors_origins == [
            "https://cbioportal.mskcc.org",
            "https://triage.cbioportal.mskcc.org",
        ]
