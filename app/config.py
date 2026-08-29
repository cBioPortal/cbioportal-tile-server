import configparser
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .constants import DEFAULT_WAREHOUSE_ID as _DEFAULT_WAREHOUSE_ID


def _aws_profile(key: str, fallback: str = "") -> str:
    """Read a value from the [ecs] section of ~/.aws/credentials, if present."""
    try:
        cfg = configparser.ConfigParser()
        cfg.read(os.path.expanduser("~/.aws/credentials"))
        return cfg.get("ecs", key, fallback=fallback)
    except Exception:
        return fallback


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() != "false"


def _env_csv(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def _env_json_file_map(name: str) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    path = Path(raw)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{name} must point to a JSON object")
    return {str(key): str(value) for key, value in data.items()}


@dataclass
class Settings:
    wsi_auth_secret: str = field(default_factory=lambda: _env_str("WSI_AUTH_SECRET"))
    wsi_auth_audience: str = field(default_factory=lambda: _env_str("WSI_AUTH_AUDIENCE", "cbioportal-wsi"))
    # Deprecated compatibility setting. Pixel routes always require a v2
    # capability; this value is intentionally ignored by app.main.
    wsi_auth_required: bool = field(default_factory=lambda: _env_bool("WSI_AUTH_REQUIRED", True))
    wsi_auth_max_ttl: int = field(default_factory=lambda: _env_int("WSI_AUTH_MAX_TTL", 300))
    wsi_allowed_source_schemes: list[str] = field(
        default_factory=lambda: _env_csv("WSI_ALLOWED_SOURCE_SCHEMES", "s3")
    )
    wsi_study_mapping_table: str = field(default_factory=lambda: _env_str("WSI_STUDY_MAPPING_TABLE"))

    aws_endpoint_url: str = field(default_factory=lambda: _env_str("AWS_ENDPOINT_URL", _aws_profile("endpoint_url", "")))
    aws_access_key_id: str = field(default_factory=lambda: _env_str("AWS_ACCESS_KEY_ID", _aws_profile("aws_access_key_id")))
    aws_secret_access_key: str = field(default_factory=lambda: _env_str("AWS_SECRET_ACCESS_KEY", _aws_profile("aws_secret_access_key")))

    tile_size: int = field(default_factory=lambda: _env_int("TILE_SIZE", 256))
    jpeg_quality: int = field(default_factory=lambda: _env_int("JPEG_QUALITY", 85))
    max_decode_pixels: int = field(default_factory=lambda: _env_int("MAX_DECODE_PIXELS", 16_777_216))
    thumbnail_max_decode_pixels: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_MAX_DECODE_PIXELS", 16_777_216)
    )
    thumbnail_timeout_sec: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_TIMEOUT_SEC", 8)
    )
    thumbnail_placeholder_cache_ttl: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_PLACEHOLDER_CACHE_TTL", 60)
    )
    thumbnail_manifest_uri: str = field(
        default_factory=lambda: _env_str("THUMBNAIL_MANIFEST_URI")
    )
    thumbnail_master_size: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_MASTER_SIZE", 1024)
    )
    thumbnail_manifest_refresh_sec: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_MANIFEST_REFRESH_SEC", 300)
    )
    thumbnail_generated_record_cache_capacity: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_GENERATED_RECORD_CACHE_CAPACITY", 4096)
    )
    thumbnail_batch_timeout_sec: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_BATCH_TIMEOUT_SEC", 600)
    )
    thumbnail_fetch_concurrency: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_FETCH_CONCURRENCY", 8)
    )
    thumbnail_fetch_max_attempts: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_FETCH_MAX_ATTEMPTS", 2)
    )
    thumbnail_fetch_retry_delay_sec: float = field(
        default_factory=lambda: _env_float("THUMBNAIL_FETCH_RETRY_DELAY_SEC", 0.1)
    )
    thumbnail_s3_max_connections: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_S3_MAX_CONNECTIONS", 32)
    )
    thumbnail_s3_connect_timeout_sec: float = field(
        default_factory=lambda: _env_float("THUMBNAIL_S3_CONNECT_TIMEOUT_SEC", 1.0)
    )
    thumbnail_s3_read_timeout_sec: float = field(
        default_factory=lambda: _env_float("THUMBNAIL_S3_READ_TIMEOUT_SEC", 5.0)
    )
    thumbnail_s3_max_attempts: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_S3_MAX_ATTEMPTS", 2)
    )
    thumbnail_prewarm_uri: str = field(
        default_factory=lambda: _env_str("THUMBNAIL_PREWARM_URI")
    )

    # Redis tile cache
    redis_url: str = field(default_factory=lambda: _env_str("REDIS_URL", "redis://redis:6379"))
    redis_connect_timeout_seconds: float = field(
        default_factory=lambda: _env_float("REDIS_CONNECT_TIMEOUT_SECONDS", 0.25)
    )
    redis_command_timeout_seconds: float = field(
        default_factory=lambda: _env_float("REDIS_COMMAND_TIMEOUT_SECONDS", 0.25)
    )
    redis_failure_backoff_seconds: float = field(
        default_factory=lambda: _env_float("REDIS_FAILURE_BACKOFF_SECONDS", 5.0)
    )
    tile_cache_ttl: int = field(default_factory=lambda: _env_int("TILE_CACHE_TTL", 86_400))
    thumbnail_cache_ttl: int = field(default_factory=lambda: _env_int("THUMBNAIL_CACHE_TTL", 86_400))
    cache_miss_lock_ttl_seconds: int = field(
        default_factory=lambda: _env_int("CACHE_MISS_LOCK_TTL_SECONDS", 120)
    )
    cache_miss_wait_timeout_seconds: float = field(
        default_factory=lambda: _env_float("CACHE_MISS_WAIT_TIMEOUT_SECONDS", 60.0)
    )
    # Deprecated cache setting retained for offline/legacy imports; runtime
    # endpoints never cache clinical or slide metadata.
    metadata_cache_ttl: int = field(default_factory=lambda: _env_int("METADATA_CACHE_TTL", 86_400))

    # Slide cache / workers
    max_open_slides: int = field(default_factory=lambda: _env_int("MAX_OPEN_SLIDES", 64))
    n_workers: int = field(default_factory=lambda: _env_int("N_WORKERS", 4))
    max_image_operations: int = field(default_factory=lambda: _env_int("MAX_IMAGE_OPERATIONS", 2))
    image_operation_queue_timeout_seconds: float = field(
        default_factory=lambda: _env_float("IMAGE_OPERATION_QUEUE_TIMEOUT_SECONDS", 2.0)
    )
    slide_s3_connect_timeout_seconds: float = field(
        default_factory=lambda: _env_float("SLIDE_S3_CONNECT_TIMEOUT_SECONDS", 1.0)
    )
    slide_s3_read_timeout_seconds: float = field(
        default_factory=lambda: _env_float("SLIDE_S3_READ_TIMEOUT_SECONDS", 10.0)
    )
    slide_s3_max_attempts: int = field(
        default_factory=lambda: _env_int("SLIDE_S3_MAX_ATTEMPTS", 2)
    )
    slide_s3_max_connections: int = field(
        default_factory=lambda: _env_int("SLIDE_S3_MAX_CONNECTIONS", 16)
    )
    # Deprecated compatibility setting retained for offline callers.
    path_cache_capacity: int = field(default_factory=lambda: _env_int("PATH_CACHE_CAPACITY", 4_096))
    # RATE_LIMIT_PER_MINUTE is retained as a one-release compatibility alias.
    cache_miss_rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int(
            "CACHE_MISS_RATE_LIMIT_PER_MINUTE",
            _env_int("RATE_LIMIT_PER_MINUTE", 120),
        )
    )

    # Offline preparation tooling only (never read by the FastAPI runtime).
    databricks_warehouse_id: str = field(
        default_factory=lambda: _env_str("DATABRICKS_WAREHOUSE_ID", _DEFAULT_WAREHOUSE_ID)
    )
    use_canonical_association_table: bool = field(
        default_factory=lambda: _env_bool("USE_CANONICAL_ASSOCIATION_TABLE", True)
    )
    allow_legacy_association_fallback: bool = field(
        default_factory=lambda: _env_bool("ALLOW_LEGACY_ASSOCIATION_FALLBACK", False)
    )
    patient_cache_ttl: int = field(default_factory=lambda: _env_int("PATIENT_CACHE_TTL", 86_400))
    blockcache_path: str = field(default_factory=lambda: _env_str("BLOCKCACHE_PATH", ""))
    blockcache_block_size: int = field(default_factory=lambda: _env_int("BLOCKCACHE_BLOCK_SIZE", 8 * 1024 * 1024))
    blockcache_max_bytes: int = field(default_factory=lambda: _env_int("BLOCKCACHE_MAX_BYTES", 0))
    blockcache_prune_interval_seconds: int = field(
        default_factory=lambda: _env_int("BLOCKCACHE_PRUNE_INTERVAL_SECONDS", 60)
    )

    # Test-only local slide fixtures
    test_slide_map_file: str = field(default_factory=lambda: _env_str("WSI_TEST_SLIDE_MAP_FILE", ""))
    test_slide_map: dict[str, str] = field(default_factory=lambda: _env_json_file_map("WSI_TEST_SLIDE_MAP_FILE"))

    annotation_database_url: str = field(default_factory=lambda: _env_str("ANNOTATION_DATABASE_URL"))
    annotation_db_path: str = field(default_factory=lambda: _env_str("ANNOTATION_DB_PATH", "/data/annotations.db"))
    keycloak_jwks_url: str = field(default_factory=lambda: _env_str("KEYCLOAK_JWKS_URL"))
    annotation_auth_enabled: bool = field(default_factory=lambda: _env_bool("ANNOTATION_AUTH_ENABLED", True))
    oncokb_api_token: str = field(default_factory=lambda: _env_str("ONCOKB_API_TOKEN"))
    agent_enabled: bool = field(default_factory=lambda: _env_bool("WSI_AGENT_ENABLED", False))
    agent_model: str = field(default_factory=lambda: _env_str("OPENAI_MODEL", "gpt-5.6-terra"))
    agent_api_key_file: str = field(default_factory=lambda: _env_str("OPENAI_API_KEY_FILE"))
    agent_timeout_seconds: float = field(default_factory=lambda: _env_float("WSI_AGENT_TIMEOUT_SECONDS", 60.0))
    agent_rate_limit_per_minute: int = field(default_factory=lambda: _env_int("WSI_AGENT_RATE_LIMIT_PER_MINUTE", 10))

    cors_origins: list[str] = field(
        default_factory=lambda: _env_csv(
            "CORS_ORIGINS",
            "https://cbioportal.mskcc.org,https://triage.cbioportal.mskcc.org",
        )
    )


settings = Settings()
