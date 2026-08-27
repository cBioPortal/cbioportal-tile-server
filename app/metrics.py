import os
from contextlib import asynccontextmanager

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

ACTIVE_IMAGE_OPERATIONS = Gauge(
    "tile_server_active_image_operations",
    "Number of image operations currently running inside a worker.",
    multiprocess_mode="livesum",
)
ACTIVE_THUMBNAIL_FETCHES = Gauge(
    "tile_server_active_thumbnail_fetches",
    "Number of thumbnail object fetches currently running inside a worker.",
    multiprocess_mode="livesum",
)
DECODE_SOURCE_PIXELS = Histogram(
    "tile_server_decode_source_pixels",
    "Source pixels decoded for a tile or thumbnail request.",
    buckets=(0, 65_536, 262_144, 1_048_576, 4_194_304, 8_388_608, 16_777_216, float("inf")),
)
OVERSIZED_DECODE_REJECTIONS = Counter(
    "tile_server_oversized_decode_rejections_total",
    "Decode requests rejected because they exceeded the configured pixel budget.",
    ("kind",),
)
CACHE_MISS_LEADERS = Counter(
    "tile_server_cache_miss_leaders_total",
    "Cache misses that became the leader request for extraction.",
    ("kind",),
)
COALESCED_CACHE_MISS_REQUESTS = Counter(
    "tile_server_coalesced_cache_miss_requests_total",
    "Requests that joined an in-flight extraction instead of starting a duplicate decode.",
    ("kind",),
)
CACHE_REQUESTS = Counter(
    "tile_server_cache_requests_total",
    "Cache requests by kind and result.",
    ("kind", "result"),
)
REDIS_OPERATION_SECONDS = Histogram(
    "tile_server_redis_operation_seconds",
    "Redis operation latency.",
    ("operation",),
)
REDIS_ERRORS = Counter(
    "tile_server_redis_errors_total",
    "Redis operation failures.",
    ("operation",),
)
SLIDE_CACHE_EVENTS = Counter(
    "tile_server_slide_cache_events_total",
    "Slide-cache lifecycle events.",
    ("event",),
)
IMAGE_OPERATION_SECONDS = Histogram(
    "tile_server_image_operation_seconds",
    "Image operation latency including time waiting for the worker gate.",
    ("kind",),
)
IMAGE_OPERATION_QUEUE_SECONDS = Histogram(
    "tile_server_image_operation_queue_seconds",
    "Time spent waiting for an image-operation slot.",
    ("kind",),
)
IMAGE_OPERATION_QUEUE_TIMEOUTS = Counter(
    "tile_server_image_operation_queue_timeouts_total",
    "Image operations rejected after waiting too long for a worker slot.",
    ("kind",),
)
SLIDE_OPEN_SECONDS = Histogram(
    "tile_server_slide_open_seconds",
    "Time spent opening a slide through the block cache and ECS.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, float("inf")),
)
SLIDE_OPEN_ERRORS = Counter(
    "tile_server_slide_open_errors_total",
    "Slide-open failures by coarse exception type.",
    ("error_type",),
)
THUMBNAIL_FETCH_SECONDS = Histogram(
    "tile_server_thumbnail_fetch_seconds",
    "Time spent fetching a thumbnail object from object storage.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, float("inf")),
)
THUMBNAIL_FETCH_QUEUE_SECONDS = Histogram(
    "tile_server_thumbnail_fetch_queue_seconds",
    "Time spent waiting for a thumbnail object-fetch slot.",
    buckets=(0.001, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, float("inf")),
)
THUMBNAIL_RESIZE_SECONDS = Histogram(
    "tile_server_thumbnail_resize_seconds",
    "Time spent resizing a fetched thumbnail.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, float("inf")),
)
THUMBNAIL_FETCH_ERRORS = Counter(
    "tile_server_thumbnail_fetch_errors_total",
    "Thumbnail object-storage fetch failures.",
)
DISTRIBUTED_MISS_LOCKS = Counter(
    "tile_server_distributed_miss_locks_total",
    "Distributed cache-miss lock outcomes.",
    ("kind", "result"),
)
CACHE_MISS_RATE_LIMITS = Counter(
    "tile_server_cache_miss_rate_limits_total",
    "Distributed cache-miss rate-limit outcomes.",
    ("result",),
)


@asynccontextmanager
async def track_image_operation():
    ACTIVE_IMAGE_OPERATIONS.inc()
    try:
        yield
    finally:
        ACTIVE_IMAGE_OPERATIONS.dec()


@asynccontextmanager
async def track_thumbnail_fetch():
    ACTIVE_THUMBNAIL_FETCHES.inc()
    try:
        yield
    finally:
        ACTIVE_THUMBNAIL_FETCHES.dec()


def metrics_payload() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import CollectorRegistry, multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
