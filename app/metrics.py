import os
from contextlib import asynccontextmanager

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

ACTIVE_IMAGE_OPERATIONS = Gauge(
    "tile_server_active_image_operations",
    "Number of image operations currently running inside a worker.",
    multiprocess_mode="livesum",
)
DECODE_SOURCE_PIXELS = Histogram(
    "tile_server_decode_source_pixels",
    "Source pixels decoded for a tile or thumbnail request.",
    buckets=(0, 65_536, 262_144, 1_048_576, 4_194_304, 8_388_608, float("inf")),
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
    "Blocking image operation latency.",
    ("kind",),
)


@asynccontextmanager
async def track_image_operation():
    ACTIVE_IMAGE_OPERATIONS.inc()
    try:
        yield
    finally:
        ACTIVE_IMAGE_OPERATIONS.dec()


def metrics_payload() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import CollectorRegistry, multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
