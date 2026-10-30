import configparser
import os
from dataclasses import dataclass, field


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


@dataclass
class Settings:
    release_id: str = field(default_factory=lambda: _env_str("WSI_RELEASE_ID"))
    image_git_sha: str = field(default_factory=lambda: _env_str("IMAGE_GIT_SHA"))
    serving_contract_version: str = field(
        default_factory=lambda: _env_str("WSI_SERVING_CONTRACT_VERSION", "wsi-serving-v6")
    )
    # WSI request authentication
    wsi_auth_secret: str = field(default_factory=lambda: _env_str("WSI_AUTH_SECRET"))
    wsi_auth_previous_secret: str = field(
        default_factory=lambda: _env_str("WSI_AUTH_PREVIOUS_SECRET")
    )
    # Standard base64 of the 32-byte AES-256-GCM key that opens the `enc`
    # claim. Shared only with the data provider that seals slide sources; the
    # cBioPortal backend never holds it. Required: startup fails without it.
    wsi_source_seal_key: str = field(
        default_factory=lambda: _env_str("WSI_SOURCE_SEAL_KEY"), repr=False
    )
    wsi_auth_audience: str = field(default_factory=lambda: _env_str("WSI_AUTH_AUDIENCE", "cbioportal-wsi"))
    wsi_auth_max_ttl: int = field(default_factory=lambda: _env_int("WSI_AUTH_MAX_TTL", 300))
    wsi_allowed_source_schemes: list[str] = field(
        default_factory=lambda: _env_csv("WSI_ALLOWED_SOURCE_SCHEMES", "s3")
    )
    # URI prefixes are an additional publication/privacy boundary. Keep these
    # empty for generic local development; production deployments must set
    # both allowlists explicitly before serving tiles.
    wsi_allowed_source_prefixes: list[str] = field(
        default_factory=lambda: _env_csv("WSI_ALLOWED_SOURCE_PREFIXES", "")
    )
    wsi_allowed_thumbnail_prefixes: list[str] = field(
        default_factory=lambda: _env_csv("WSI_ALLOWED_THUMBNAIL_PREFIXES", "")
    )

    aws_endpoint_url: str = field(default_factory=lambda: _env_str("AWS_ENDPOINT_URL", _aws_profile("endpoint_url", "")))
    aws_access_key_id: str = field(default_factory=lambda: _env_str("AWS_ACCESS_KEY_ID", _aws_profile("aws_access_key_id")))
    aws_secret_access_key: str = field(default_factory=lambda: _env_str("AWS_SECRET_ACCESS_KEY", _aws_profile("aws_secret_access_key")))

    tile_size: int = field(default_factory=lambda: _env_int("TILE_SIZE", 256))
    jpeg_quality: int = field(default_factory=lambda: _env_int("JPEG_QUALITY", 85))
    max_decode_pixels: int = field(default_factory=lambda: _env_int("MAX_DECODE_PIXELS", 16_777_216))
    thumbnail_max_decode_pixels: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_MAX_DECODE_PIXELS", 16_777_216)
    )
    # Read only by the offline thumbnail pipeline that imports this package.
    thumbnail_manifest_uri: str = field(
        default_factory=lambda: _env_str("THUMBNAIL_MANIFEST_URI")
    )
    thumbnail_master_size: int = field(
        default_factory=lambda: _env_int("THUMBNAIL_MASTER_SIZE", 1024)
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
    thumbnail_prewarm_required: bool = field(
        default_factory=lambda: _env_bool("THUMBNAIL_PREWARM_REQUIRED", False)
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
    # RATE_LIMIT_PER_MINUTE is retained as a one-release compatibility alias.
    cache_miss_rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int(
            "CACHE_MISS_RATE_LIMIT_PER_MINUTE",
            _env_int("RATE_LIMIT_PER_MINUTE", 120),
        )
    )

    # Block cache
    blockcache_path: str = field(default_factory=lambda: _env_str("BLOCKCACHE_PATH", ""))
    blockcache_block_size: int = field(default_factory=lambda: _env_int("BLOCKCACHE_BLOCK_SIZE", 8 * 1024 * 1024))
    blockcache_max_bytes: int = field(default_factory=lambda: _env_int("BLOCKCACHE_MAX_BYTES", 0))
    blockcache_prune_interval_seconds: int = field(
        default_factory=lambda: _env_int("BLOCKCACHE_PRUNE_INTERVAL_SECONDS", 60)
    )

    annotation_database_url: str = field(default_factory=lambda: _env_str("ANNOTATION_DATABASE_URL"))
    annotation_db_path: str = field(default_factory=lambda: _env_str("ANNOTATION_DB_PATH", "/data/annotations.db"))
    annotation_auth_enabled: bool = field(default_factory=lambda: _env_bool("ANNOTATION_AUTH_ENABLED", True))
    annotation_local_development: bool = field(
        default_factory=lambda: _env_bool("ANNOTATION_LOCAL_DEVELOPMENT", False)
    )
    agent_enabled: bool = field(default_factory=lambda: _env_bool("WSI_AGENT_ENABLED", False))
    agent_provider: str = field(default_factory=lambda: _env_str("WSI_AGENT_PROVIDER", "bedrock"))
    # WSI_AGENT_MODEL names the model for any provider (a Bedrock model ID,
    # an OpenAI model, or a Databricks serving endpoint name);
    # BEDROCK_AGENT_MODEL is read when it is unset.
    agent_model: str = field(
        default_factory=lambda: _env_str("WSI_AGENT_MODEL")
        or _env_str("BEDROCK_AGENT_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    )
    agent_region: str = field(default_factory=lambda: _env_str("BEDROCK_AWS_REGION", "us-east-1"))
    agent_profile: str = field(default_factory=lambda: _env_str("BEDROCK_AWS_PROFILE"))
    agent_api_key_file: str = field(default_factory=lambda: _env_str("OPENAI_API_KEY_FILE"))
    # WSI_AGENT_PROVIDER=databricks: the workspace serves the OpenAI
    # Responses API at <host>/serving-endpoints. Host and token come from
    # these variables, else from a profile of the Databricks CLI config file.
    databricks_host: str = field(default_factory=lambda: _env_str("DATABRICKS_HOST"))
    databricks_token: str = field(
        default_factory=lambda: _env_str("DATABRICKS_TOKEN"), repr=False
    )
    databricks_token_file: str = field(default_factory=lambda: _env_str("DATABRICKS_TOKEN_FILE"))
    databricks_config_file: str = field(
        default_factory=lambda: _env_str("DATABRICKS_CONFIG_FILE", "~/.databrickscfg")
    )
    databricks_config_profile: str = field(
        default_factory=lambda: _env_str("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
    )
    research_enabled: bool = field(default_factory=lambda: _env_bool("WSI_RESEARCH_ENABLED", False))
    research_manifest_uri: str = field(default_factory=lambda: _env_str("WSI_RESEARCH_MANIFEST_URI"))
    research_rate_limit_per_minute: int = field(
        default_factory=lambda: _env_int("WSI_RESEARCH_RATE_LIMIT_PER_MINUTE", 30)
    )
    # Region retrieval runs in a separate worker process because its models
    # and feature tensors are much larger than the pixel-serving workers.
    research_embedding_url: str = field(
        default_factory=lambda: _env_str("WSI_RESEARCH_EMBEDDING_URL")
    )
    # Optional per-model worker URLs, "model=url,model=url"; a model not
    # listed here uses WSI_RESEARCH_EMBEDDING_URL.
    research_embedding_urls: str = field(
        default_factory=lambda: _env_str("WSI_RESEARCH_EMBEDDING_URLS")
    )
    # The tile-level model used for text-to-region search (the viewer's
    # region-search default and the assistant's wsi_find_regions tool).
    research_region_model: str = field(
        default_factory=lambda: _env_str("WSI_RESEARCH_REGION_MODEL", "quiltnet_pmb")
    )
    research_embedding_timeout_seconds: float = field(
        default_factory=lambda: _env_float("WSI_RESEARCH_EMBEDDING_TIMEOUT_SECONDS", 60.0)
    )
    research_embedding_in_process: bool = field(
        default_factory=lambda: _env_bool("WSI_RESEARCH_EMBEDDING_IN_PROCESS", False)
    )
    research_static_fallback: bool = field(
        default_factory=lambda: _env_bool("WSI_RESEARCH_STATIC_FALLBACK", True)
    )
    research_embedding_cache_dir: str = field(
        default_factory=lambda: _env_str("WSI_RESEARCH_EMBEDDING_CACHE_DIR", "/cache/research-embeddings")
    )
    research_embedding_cache_max_bytes: int = field(
        default_factory=lambda: _env_int(
            "WSI_RESEARCH_EMBEDDING_CACHE_MAX_BYTES", 20 * 1024**3
        )
    )
    quiltnet_prompt_cache_size: int = field(
        default_factory=lambda: _env_int("QUILTNET_PROMPT_CACHE_SIZE", 4096)
    )
    quiltnet_score_chunk_size: int = field(
        default_factory=lambda: _env_int("QUILTNET_SCORE_CHUNK_SIZE", 65_536)
    )
    quiltnet_torch_threads: int = field(
        default_factory=lambda: _env_int("QUILTNET_TORCH_THREADS", 2)
    )
    quiltnet_torch_interop_threads: int = field(
        default_factory=lambda: _env_int("QUILTNET_TORCH_INTEROP_THREADS", 1)
    )
    quiltnet_max_concurrent_searches: int = field(
        default_factory=lambda: _env_int("QUILTNET_MAX_CONCURRENT_SEARCHES", 1)
    )
    quiltnet_max_queue: int = field(
        default_factory=lambda: _env_int("QUILTNET_MAX_QUEUE", 8)
    )
    quiltnet_queue_timeout_seconds: float = field(
        default_factory=lambda: _env_float("QUILTNET_QUEUE_TIMEOUT_SECONDS", 30.0)
    )
    quiltnet_model_name: str = field(
        default_factory=lambda: _env_str(
            "QUILTNET_MODEL_NAME", "hf-hub:wisdomik/QuiltNet-B-16-PMB"
        )
    )
    quiltnet_device: str = field(
        default_factory=lambda: _env_str("QUILTNET_DEVICE", "auto")
    )
    agent_timeout_seconds: float = field(default_factory=lambda: _env_float("WSI_AGENT_TIMEOUT_SECONDS", 60.0))
    agent_rate_limit_per_minute: int = field(default_factory=lambda: _env_int("WSI_AGENT_RATE_LIMIT_PER_MINUTE", 10))

    # CORS
    cors_origins: list[str] = field(
        default_factory=lambda: _env_csv(
            "CORS_ORIGINS",
            "https://cbioportal.mskcc.org,https://triage.cbioportal.mskcc.org",
        )
    )


settings = Settings()
