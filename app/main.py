"""
Tile server — FastAPI application.

Endpoints:
  GET /health
  GET /tiles/{slide_id}/metadata
  GET /thumbnails/{slide_id}?width=256&height=256
  GET /tiles/{slide_id}/zxy/{z}/{x}/{y}

All tile and thumbnail responses carry long-lived Cache-Control headers so a
CDN or nginx proxy_cache can absorb the bulk of repeat requests.
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, suppress
from pathlib import Path

# Ensure app.* loggers emit to stderr alongside uvicorn's own loggers.
# uvicorn's dictConfig only configures uvicorn.* — root logger has no handler
# by default, so INFO from app.* would be silently dropped without this.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from . import cache as tile_cache
from . import meta
from .auth import InvalidWsiToken, validate_wsi_token
from .blockcache import get_blockcache_manager
from .config import settings
from .metrics import (
    CACHE_MISS_LEADERS,
    COALESCED_CACHE_MISS_REQUESTS,
    DECODE_SOURCE_PIXELS,
    IMAGE_OPERATION_SECONDS,
    OVERSIZED_DECODE_REJECTIONS,
    metrics_payload,
    track_image_operation,
)
from .associations import build_specimen_key, derive_block_fields
from .meta import get_slide_dbmeta, search_suggestions
from .meta_store import get_patient_association_rows
from .rate_limit import EXPENSIVE_PATH_PREFIXES, rate_limiter
from .resource_index import ResourceIndexUnavailable, get_resource_index
from .slides import SlideCache
from .thumbnail_store import (
    ThumbnailRecord,
    get_persisted_generated_thumbnail_record,
    get_thumbnail_record,
    render_thumbnail_response,
)
from .tiles import OverviewTooLarge, get_tile_bytes, render_tile_image, slide_metadata
from .tiles import get_placeholder_thumbnail_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_slides: SlideCache | None = None
# In-process cache: image_id → s3 URI (populated on first open, survives across requests)
_path_cache: OrderedDict[str, str] = OrderedDict()
_path_cache_lock = threading.Lock()
_image_operation_semaphore: asyncio.Semaphore | None = None
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _SingleFlight:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._futures: dict[str, asyncio.Future] = {}

    async def do(self, key: str, kind: str, producer):
        async with self._lock:
            future = self._futures.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._futures[key] = future
                leader = True
                CACHE_MISS_LEADERS.labels(kind=kind).inc()
            else:
                leader = False
                COALESCED_CACHE_MISS_REQUESTS.labels(kind=kind).inc()

        if not leader:
            return await future

        try:
            result = await producer()
        except Exception as exc:
            future.set_exception(exc)
            future.exception()
            raise
        else:
            future.set_result(result)
            return result
        finally:
            async with self._lock:
                self._futures.pop(key, None)


_singleflight = _SingleFlight()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _slides, _image_operation_semaphore
    _slides = SlideCache(capacity=settings.max_open_slides)
    _image_operation_semaphore = asyncio.Semaphore(settings.max_image_operations)
    await tile_cache.init_cache()
    if settings.wsi_auth_required and settings.wsi_resource_index_file:
        try:
            await _in_thread(
                get_resource_index(settings.wsi_resource_index_file).revision
            )
        except ResourceIndexUnavailable:
            logger.error("WSI resource index is unavailable at startup; protected requests will fail closed")
    blockcache_manager = get_blockcache_manager()
    blockcache_task = None
    if blockcache_manager.enabled:
        await _in_thread(blockcache_manager.prune)

        async def _prune_loop():
            while True:
                await asyncio.sleep(blockcache_manager.prune_interval_seconds)
                await _in_thread(blockcache_manager.prune)

        blockcache_task = asyncio.create_task(_prune_loop())
    if not settings.aws_endpoint_url:
        logger.warning(
            "AWS_ENDPOINT_URL is not set — S3 requests will go to public AWS "
            "(set this to your Dell ECS endpoint in production)"
        )
    logger.info(
        "Tile server ready. max_open_slides=%d endpoint=%s",
        settings.max_open_slides,
        settings.aws_endpoint_url or "AWS default",
    )
    try:
        yield
    finally:
        if blockcache_task is not None:
            blockcache_task.cancel()
            with suppress(asyncio.CancelledError):
                await blockcache_task
        _slides.close_all()
        await tile_cache.close_cache()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WSI Tile Server",
    description="Serve SVS whole-slide image tiles directly from Dell ECS (S3) via tiffslide.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.middleware("http")
async def limit_expensive_requests(request: Request, call_next):
    path = request.scope["path"]
    if (
        settings.wsi_auth_required
        and path.startswith(EXPENSIVE_PATH_PREFIXES)
        and (payload := getattr(request.state, "wsi_token_payload", None)) is not None
        and not rate_limiter.allow(
            payload["sub"],
            settings.rate_limit_per_minute,
        )
    ):
        return Response(
            status_code=429,
            headers={"Retry-After": "60"},
            content="Rate limit exceeded",
        )
    return await call_next(request)


@app.middleware("http")
async def require_wsi_capability(request: Request, call_next):
    """Require a cBioPortal-issued capability for every non-health API request."""
    if request.scope["path"] in ("/health", "/wsi/health") or request.scope["path"].startswith(
        "/internal/patient/"
    ):
        return await call_next(request)
    if not settings.wsi_auth_required:
        return await call_next(request)

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = validate_wsi_token(
            authorization[7:].strip(),
            settings.wsi_auth_secret,
            settings.wsi_auth_audience,
            settings.wsi_auth_max_ttl,
        )
    except InvalidWsiToken:
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    request.state.wsi_token_payload = claims
    request.state.wsi_claims = claims
    return await call_next(request)


@app.middleware("http")
async def wsi_namespace(request, call_next):
    """Expose the API under /wsi without changing its internal route paths."""
    path = request.scope["path"]
    if path == "/wsi" or path.startswith("/wsi/"):
        request.scope["path"] = path[4:] or "/"
    return await call_next(request)

TILE_CACHE_HEADERS  = {"Cache-Control": "private, max-age=3600", "Vary": "Authorization"}
THUMB_CACHE_HEADERS = {"Cache-Control": "private, max-age=300", "Vary": "Authorization"}
# Metadata and search responses contain patient/slide information.
PHI_CACHE_HEADERS   = {"Cache-Control": "private, no-store", "Vary": "Authorization"}
THUMBNAIL_UNAVAILABLE_PLACEHOLDER_TTL = 60


async def _in_thread(fn, *args):
    """Run a blocking function in the default thread-pool executor."""
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def _run_image_operation(fn, *args, operation_kind: str = "image"):
    if _image_operation_semaphore is None:
        started = time.perf_counter()
        try:
            return await _in_thread(fn, *args)
        finally:
            IMAGE_OPERATION_SECONDS.labels(kind=operation_kind).observe(time.perf_counter() - started)
    async with _image_operation_semaphore:
        async with track_image_operation():
            started = time.perf_counter()
            try:
                return await _in_thread(fn, *args)
            finally:
                IMAGE_OPERATION_SECONDS.labels(kind=operation_kind).observe(time.perf_counter() - started)


def _thumbnail_status_headers(status: str, reason: str) -> dict[str, str]:
    return {
        "X-Thumbnail-Status": status,
        "X-Thumbnail-Reason": reason,
    }


def _thumbnail_placeholder_ttl(reason: str) -> int | None:
    if reason == "unavailable":
        if settings.thumbnail_cache_ttl:
            return min(settings.thumbnail_cache_ttl, THUMBNAIL_UNAVAILABLE_PLACEHOLDER_TTL)
        return THUMBNAIL_UNAVAILABLE_PLACEHOLDER_TTL
    return None


def _log_thumbnail_outcome(
    *,
    slide_id: str,
    width: int,
    height: int,
    elapsed_ms: float,
    outcome: str,
    reason: str,
) -> None:
    logger.info(
        "thumbnail_request slide_id=%s width=%d height=%d elapsed_ms=%.1f outcome=%s reason=%s",
        slide_id,
        width,
        height,
        elapsed_ms,
        outcome,
        reason,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_slide_id(image_id: str, study_id: str | None = None) -> str:
    """Resolve an image_id to a local path or S3 URI, with in-process caching."""
    cache_key = f"{study_id}:{image_id}" if study_id else image_id
    with _path_cache_lock:
        if cache_key in _path_cache:
            _path_cache.move_to_end(cache_key)
            return _path_cache[cache_key]

    def cache_path(path: str) -> str:
        with _path_cache_lock:
            _path_cache[cache_key] = path
            _path_cache.move_to_end(cache_key)
            while len(_path_cache) > settings.path_cache_capacity:
                _path_cache.popitem(last=False)
        return path

    test_path = settings.test_slide_map.get(image_id)
    if test_path:
        return cache_path(test_path)
    if study_id:
        binding = get_resource_index(settings.wsi_resource_index_file).slide_binding(study_id, image_id)
        if binding is None:
            raise ResourceIndexUnavailable(
                "study-qualified slide binding is missing"
            )
        if not binding.get("source_path"):
            raise FileNotFoundError(f"Slide not found: {image_id}")
        path = str(binding["source_path"])
        return cache_path(path)
    path = meta.get_slide_path(image_id, settings.databricks_warehouse_id)
    if not path:
        raise FileNotFoundError(f"Slide not found: {image_id}")
    return cache_path(path)


def _get_slide(image_id: str, study_id: str | None = None):
    """Resolve image_id → S3 path, open/retrieve from cache; raise 404 on failure."""
    try:
        s3_uri = _resolve_slide_id(image_id, study_id)
        return _slides.get(s3_uri)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Slide not found: {image_id}")
    except ResourceIndexUnavailable:
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    except Exception:
        logger.exception("Failed to open slide %s", image_id)
        raise HTTPException(status_code=500, detail="Failed to open slide")


def _run_slide_operation(image_id: str, study_id: str | None, operation, *args):
    """Resolve and run one blocking image operation under a slide lease."""
    try:
        s3_uri = _resolve_slide_id(image_id, study_id)
        if isinstance(_slides, SlideCache):
            return _slides.run(s3_uri, operation, *args)
        # Test doubles and embedding callers may provide the legacy cache
        # interface. Production always uses SlideCache.run above.
        slide = _slides.get(s3_uri)
        return operation(slide, *args)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Slide not found: {image_id}")
    except ResourceIndexUnavailable:
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    except (HTTPException, OverviewTooLarge, ValueError):
        raise
    except Exception:
        logger.exception("Slide operation failed for %s", image_id)
        raise HTTPException(status_code=500, detail="Slide operation failed")


async def _run_slide_image_operation(
    image_id: str,
    study_id: str | None,
    operation,
    *args,
    operation_kind: str = "image",
):
    return await _run_image_operation(
        _run_slide_operation,
        image_id,
        study_id,
        operation,
        *args,
        operation_kind=operation_kind,
    )


async def _generate_thumbnail_record_on_demand(
    image_id: str,
    study_id: str | None,
):
    if not settings.thumbnail_manifest_uri.strip():
        return None
    try:
        source_uri = await _in_thread(_resolve_slide_id, image_id, study_id)
    except FileNotFoundError:
        return None

    if _image_operation_semaphore is None:
        return await _run_thumbnail_worker(image_id, source_uri)
    async with _image_operation_semaphore:
        async with track_image_operation():
            return await _run_thumbnail_worker(image_id, source_uri)


async def _stop_thumbnail_worker(process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.communicate()


async def _run_thumbnail_worker(image_id: str, source_uri: str):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.thumbnail_worker",
        "--image-id",
        image_id,
        "--source-uri",
        source_uri,
        "--master-size",
        str(settings.thumbnail_master_size),
        cwd=str(_REPOSITORY_ROOT),
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=settings.thumbnail_timeout_sec,
        )
    except (asyncio.CancelledError, asyncio.TimeoutError):
        await _stop_thumbnail_worker(process)
        raise

    if process.returncode != 0:
        error = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"thumbnail worker failed ({process.returncode}): {error}")
    try:
        payload = json.loads(stdout)
        return ThumbnailRecord(
            image_id=str(payload["image_id"]),
            uri=str(payload["uri"]),
            width=max(1, int(payload["width"])),
            height=max(1, int(payload["height"])),
            content_type=str(payload.get("content_type") or "image/jpeg"),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("thumbnail worker returned invalid metadata") from exc


def _authorize_resource(request: Request, resource_type: str, resource_id: str) -> str | None:
    """Bind a protected resource to the study in the validated capability."""
    if not settings.wsi_auth_required:
        return None

    claims = getattr(request.state, "wsi_claims", None)
    if not claims:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    study_id = claims["study_id"]
    requested_study = request.query_params.get("studyId")
    if requested_study is not None and requested_study != study_id:
        raise HTTPException(status_code=403, detail="study does not match capability")

    try:
        index = get_resource_index(settings.wsi_resource_index_file)
        allowed = index.contains(study_id, resource_type, str(resource_id))
    except ResourceIndexUnavailable:
        logger.error("WSI resource index is unavailable; refusing protected resource")
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    if not allowed:
        raise HTTPException(status_code=403, detail="resource is outside capability study")
    return study_id


def _authenticated_search_context(request: Request):
    claims = getattr(request.state, "wsi_claims", None)
    if not claims:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    try:
        study_id = claims["study_id"]
        index = get_resource_index(settings.wsi_resource_index_file)
        return study_id, index, index.revision()
    except ResourceIndexUnavailable:
        logger.error("WSI resource index is unavailable; refusing protected search")
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")


def _normalize_match_level(row: dict) -> str:
    match_level = str(row.get("match_level") or "").upper()
    if match_level in {"BLOCK", "PART", "UNMATCHED"}:
        return match_level
    sample_id = row.get("sample_id")
    block_id = row.get("block_id")
    if not sample_id:
        return "UNMATCHED"
    return "BLOCK" if block_id not in (None, "") else "PART"


def _normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _normalize_slide_type(row: dict) -> str:
    if _normalize_bool(row.get("is_ihc")):
        return "IHC"
    if _normalize_bool(row.get("is_hne")):
        return "H&E"
    stain_name = str(row.get("stain_name") or "")
    stain_group = str(row.get("stain_group") or "")
    haystack = f"{stain_name} {stain_group}".upper()
    return "IHC" if "IHC" in haystack else "H&E"


def _optional_int(row: dict, *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return int(value)
    return None


def _hierarchy_keys(
    row: dict, match_level: str, image_id: str
) -> tuple[str, str, str, str, str, str]:
    """Resolve normalized keys from either canonical association schema."""
    block_id = row.get("block_id")
    block_id = None if block_id in (None, "") else str(block_id)
    part_number, block_number, derived_block_label = derive_block_fields(
        block_id, row.get("block_label")
    )

    part_key = str(row.get("part_key") or "").strip()
    if not part_key:
        if block_id and "/" in block_id:
            part_key = f"part::{block_id.rsplit('/', 1)[0]}"
        elif part_number is not None:
            part_key = f"part::{part_number}"
        else:
            part_key = f"{match_level.lower()}::{row.get('part_description') or image_id}"

    block_key = str(row.get("block_key") or "").strip()
    if not block_key:
        block_key = f"block::{block_id or image_id}"

    resolved_part_number = str(row.get("part_number") or "")
    if not resolved_part_number and part_number is not None:
        resolved_part_number = str(part_number)
    resolved_block_number = str(row.get("block_number") or "")
    if not resolved_block_number:
        resolved_block_number = block_number or ""
    resolved_block_label = str(row.get("block_label") or "")
    if not resolved_block_label:
        resolved_block_label = derived_block_label or resolved_block_number

    specimen_key = str(row.get("specimen_key") or "").strip()
    if not specimen_key:
        specimen_key = build_specimen_key(match_level, part_number, resolved_block_number)

    return (
        part_key,
        block_key,
        resolved_part_number,
        resolved_block_number,
        resolved_block_label,
        specimen_key,
    )


def _build_patient_hierarchy(rows: list[dict]) -> dict:
    sample_groups: OrderedDict[str | None, dict] = OrderedDict()
    reference_sample_id = None
    seen_slides: set[str] = set()

    for row in rows:
        image_id = row.get("image_id")
        if image_id is None:
            continue
        image_id = str(image_id)
        if image_id in seen_slides:
            continue
        seen_slides.add(image_id)

        match_level = _normalize_match_level(row)
        sample_id = row.get("sample_id")
        if match_level == "UNMATCHED" or sample_id in (None, "", "UNMATCHED"):
            sample_id = None
        else:
            sample_id = str(sample_id)
            if reference_sample_id is None:
                reference_sample_id = sample_id

        row_reference_sample_id = row.get("reference_sample_id")
        if row_reference_sample_id not in (None, "", "UNMATCHED"):
            reference_sample_id = str(row_reference_sample_id)

        part_type = str(row.get("part_type") or "")
        part_description = str(row.get("part_description") or "")
        path_dx_title = str(row.get("path_dx_title") or part_description)
        (
            part_key,
            block_key,
            part_number,
            block_number,
            block_label,
            specimen_key,
        ) = _hierarchy_keys(row, match_level, image_id)

        group = sample_groups.setdefault(
            sample_id,
            {
                "sampleId": sample_id,
                "partsByKey": OrderedDict(),
            },
        )
        part = group["partsByKey"].setdefault(
            part_key,
            {
                "partNumber": part_number,
                "partDesignator": str(row.get("part_designator") or part_number),
                "partType": part_type,
                "partDescription": part_description,
                "subspecialty": str(row.get("subspecialty") or ""),
                "pathDxTitle": path_dx_title,
                "blocksByKey": OrderedDict(),
            },
        )
        block = part["blocksByKey"].setdefault(
            block_key,
            {
                "blockNumber": block_number,
                "blockLabel": block_label,
                "slides": [],
            },
        )

        slide_type = _normalize_slide_type(row)
        slide_path = row.get("slide_path")
        can_serve_tiles = (
            _normalize_bool(row.get("can_serve_tiles"))
            if row.get("can_serve_tiles") is not None
            else isinstance(slide_path, str) and slide_path.startswith("s3://")
        )

        block["slides"].append(
            {
                "imageId": image_id,
                "stainName": str(row.get("stain_name") or ""),
                "stainGroup": str(row.get("stain_group") or ""),
                "isHne": _normalize_bool(row.get("is_hne"))
                if row.get("is_hne") is not None
                else slide_type == "H&E",
                "isIhc": _normalize_bool(row.get("is_ihc"))
                if row.get("is_ihc") is not None
                else slide_type == "IHC",
                "magnification": str(row.get("magnification") or ""),
                "fileSizeBytes": _optional_int(row, "file_size_bytes"),
                "canServeTiles": can_serve_tiles,
                "barcode": str(row.get("barcode") or ""),
                "slideType": str(row.get("slide_type") or slide_type),
                "sampleId": sample_id,
                "matchLevel": match_level,
                "specimenKey": specimen_key,
                "procedureDateDays": _optional_int(
                    row, "procedure_date_days", "slide_timepoint_days"
                ),
                "timepointSource": row.get("timepoint_source")
                or row.get("slide_timepoint_source"),
            }
        )

    normalized_groups = []
    for group in sample_groups.values():
        parts = []
        for part in group["partsByKey"].values():
            blocks = list(part.pop("blocksByKey").values())
            part["blocks"] = blocks
            parts.append(part)
        normalized_groups.append({"sampleId": group["sampleId"], "parts": parts})

    return {
        "referenceSampleId": reference_sample_id,
        "sampleGroups": normalized_groups,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "n_workers": settings.n_workers}


@app.get("/metrics", include_in_schema=False)
def metrics():
    payload, content_type = metrics_payload()
    return Response(content=payload, media_type=content_type, headers=PHI_CACHE_HEADERS)


@app.get("/slides/{image_id}/dbmeta")
async def slide_dbmeta(request: Request, image_id: str):
    """Return the raw Databricks metadata row for a single slide (by numeric image_id)."""
    study_id = _authorize_resource(request, "slides", image_id)
    binding = (
        get_resource_index(settings.wsi_resource_index_file).slide_binding(study_id, image_id)
        if study_id
        else None
    )
    if study_id and binding is None:
        raise HTTPException(status_code=503, detail="WSI resource authorization is unavailable")
    try:
        result = await _in_thread(
            get_slide_dbmeta,
            image_id,
            settings.databricks_warehouse_id,
            binding.get("patient_id") if binding else None,
        )
    except Exception:
        logger.exception("Databricks query failed for slide %s", image_id)
        raise HTTPException(status_code=502, detail="Metadata query failed")

    if result is None:
        raise HTTPException(status_code=404, detail="Slide not found")
    return Response(content=json.dumps(result, default=str),
                    media_type="application/json", headers=PHI_CACHE_HEADERS)


@app.get("/search")
async def search(request: Request, q: str = ""):
    """
    Autocomplete suggestions for the search bar.

    Returns up to 8 items: [{ type, id, label, sublabel }]
    Detects query pattern: P-xxx → patients, P-xxx-Tx → samples, digits → slides.
    Results are cached in Redis for 5 minutes.
    """
    q = q.strip()
    if len(q) < 2:
        return Response(content="[]", media_type="application/json", headers=PHI_CACHE_HEADERS)

    if settings.wsi_auth_required:
        study_id, index, revision = _authenticated_search_context(request)
        cache_key = f"search:{study_id}:{revision}:{q.lower()}"
        cached = await tile_cache.get_raw(cache_key)
        if cached is not None:
            return Response(
                content=json.dumps(cached, default=str),
                media_type="application/json",
                headers=PHI_CACHE_HEADERS,
            )
        results = await _in_thread(index.suggestions, study_id, q)
        await tile_cache.set_raw(cache_key, results, ttl=300)
        return Response(
            content=json.dumps(results, default=str),
            media_type="application/json",
            headers=PHI_CACHE_HEADERS,
        )

    cache_key = f"search:{q.lower()}"
    cached = await tile_cache.get_raw(cache_key)
    if cached is not None:
        return Response(content=json.dumps(cached, default=str),
                        media_type="application/json", headers=PHI_CACHE_HEADERS)

    try:
        results = await _in_thread(
            search_suggestions,
            q,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Search query failed for %r", q)
        raise HTTPException(status_code=502, detail="Search query failed")

    await tile_cache.set_raw(cache_key, results, ttl=300)
    return Response(content=json.dumps(results, default=str),
                    media_type="application/json", headers=PHI_CACHE_HEADERS)


@app.get("/internal/patient/{patient_id}")
async def patient_hierarchy(patient_id: str):
    """Local/dev fallback hierarchy built from canonical pathology associations."""
    try:
        rows = await _in_thread(
            get_patient_association_rows,
            patient_id,
            settings.databricks_warehouse_id,
        )
    except Exception:
        logger.exception("Patient hierarchy query failed for %s", patient_id)
        raise HTTPException(status_code=502, detail="Patient hierarchy query failed")

    if not rows:
        raise HTTPException(status_code=404, detail="Patient not found")

    return Response(
        content=json.dumps(_build_patient_hierarchy(rows), default=str),
        media_type="application/json",
        headers=PHI_CACHE_HEADERS,
    )


@app.get("/tiles/{slide_id}/metadata")
async def metadata(request: Request, slide_id: str):
    study_id = _authorize_resource(request, "slides", slide_id)
    cache_key = f"{study_id}:{slide_id}" if study_id else slide_id
    cached = await tile_cache.get_metadata(cache_key)
    if cached is not None:
        return Response(content=json.dumps(cached), media_type="application/json",
                        headers=PHI_CACHE_HEADERS)
    result = await _run_slide_image_operation(
        slide_id, study_id, slide_metadata, operation_kind="metadata"
    )
    await tile_cache.set_metadata(cache_key, result)
    return Response(content=json.dumps(result), media_type="application/json",
                    headers=PHI_CACHE_HEADERS)


@app.get("/tiles/{slide_id}/warmup", include_in_schema=False)
async def warmup(request: Request, slide_id: str):
    """Fetch and discard the overview tile to prime the TiffSlide cache on this worker."""
    study_id = _authorize_resource(request, "slides", slide_id)
    try:
        def _warmup(slide):
            image, _ = render_tile_image(slide, 0, 0, 0)
            return image

        image = await _run_slide_image_operation(
            slide_id, study_id, _warmup, operation_kind="warmup"
        )
        image.close()
    except OverviewTooLarge as exc:
        logger.warning(
            "Warmup overview rejected for slide %s level=%d read=%dx%d pixels=%d limit=%d",
            slide_id,
            exc.best_level,
            exc.read_width,
            exc.read_height,
            exc.requested_pixels,
            exc.max_decode_pixels,
        )
    except Exception:
        pass
    return Response(
        content=json.dumps({"status": "ok"}),
        media_type="application/json",
        headers=PHI_CACHE_HEADERS,
    )


@app.get("/thumbnails/{slide_id}")
async def thumbnail(
    request: Request,
    slide_id: str,
    width: int = 256,
    height: int = 256,
):
    study_id = _authorize_resource(request, "slides", slide_id)
    cache_slide_id = f"{study_id}:{slide_id}" if study_id else slide_id
    width = max(1, min(width, 2048))
    height = max(1, min(height, 2048))

    cached = await tile_cache.get_thumbnail(cache_slide_id, width, height)
    if cached:
        status = await tile_cache.get_thumbnail_status(cache_slide_id, width, height)
        headers = dict(THUMB_CACHE_HEADERS)
        if status:
            headers.update(
                _thumbnail_status_headers(
                    str(status.get("status") or "ok"),
                    str(status.get("reason") or "served"),
                )
            )
        return Response(content=cached, media_type="image/jpeg", headers=headers)

    cache_key = tile_cache.thumbnail_cache_key(cache_slide_id, width, height)
    started = time.perf_counter()

    async def _build_thumbnail():
        record = await _in_thread(get_thumbnail_record, slide_id)
        if record is None:
            record = await _in_thread(get_persisted_generated_thumbnail_record, slide_id)
        if record is None:
            record = await _generate_thumbnail_record_on_demand(slide_id, study_id)
        if record is None:
            data = get_placeholder_thumbnail_bytes(width, height)
            status = {"status": "placeholder", "reason": "missing"}
        else:
            try:
                data, status = await _in_thread(render_thumbnail_response, record, width, height)
            except FileNotFoundError:
                record = await _generate_thumbnail_record_on_demand(slide_id, study_id)
                if record is None:
                    raise
                data, status = await _in_thread(render_thumbnail_response, record, width, height)
        await tile_cache.set_thumbnail(cache_slide_id, width, height, data)
        await tile_cache.set_thumbnail_status(cache_slide_id, width, height, status)
        return data, status

    try:
        data, status = await _singleflight.do(cache_key, "thumbnail", _build_thumbnail)
    except asyncio.TimeoutError:
        data = get_placeholder_thumbnail_bytes(width, height)
        status = {"status": "placeholder", "reason": "decode_timeout"}
        ttl = _thumbnail_placeholder_ttl(str(status["reason"]))
        await tile_cache.set_thumbnail(cache_slide_id, width, height, data, ttl=ttl)
        await tile_cache.set_thumbnail_status(cache_slide_id, width, height, status, ttl=ttl)
        elapsed_ms = (time.perf_counter() - started) * 1000
        _log_thumbnail_outcome(
            slide_id=slide_id,
            width=width,
            height=height,
            elapsed_ms=elapsed_ms,
            outcome=str(status["status"]),
            reason=str(status["reason"]),
        )
        return Response(
            content=data,
            media_type="image/jpeg",
            headers=dict(THUMB_CACHE_HEADERS)
            | _thumbnail_status_headers(str(status["status"]), str(status["reason"])),
        )
    except Exception:
        data = get_placeholder_thumbnail_bytes(width, height)
        status = {"status": "placeholder", "reason": "unavailable"}
        ttl = _thumbnail_placeholder_ttl(str(status["reason"]))
        await tile_cache.set_thumbnail(cache_slide_id, width, height, data, ttl=ttl)
        await tile_cache.set_thumbnail_status(cache_slide_id, width, height, status, ttl=ttl)
        elapsed_ms = (time.perf_counter() - started) * 1000
        _log_thumbnail_outcome(
            slide_id=slide_id,
            width=width,
            height=height,
            elapsed_ms=elapsed_ms,
            outcome=str(status["status"]),
            reason=str(status["reason"]),
        )
        return Response(
            content=data,
            media_type="image/jpeg",
            headers=dict(THUMB_CACHE_HEADERS)
            | _thumbnail_status_headers(str(status["status"]), str(status["reason"])),
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    _log_thumbnail_outcome(
        slide_id=slide_id,
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
        outcome=str(status["status"]),
        reason=str(status["reason"]),
    )
    return Response(
        content=data,
        media_type="image/jpeg",
        headers=dict(THUMB_CACHE_HEADERS)
        | _thumbnail_status_headers(str(status["status"]), str(status["reason"])),
    )


@app.get("/tiles/{slide_id}/zxy/{z}/{x}/{y}")
async def tile(request: Request, slide_id: str, z: int, x: int, y: int):
    study_id = _authorize_resource(request, "slides", slide_id)
    cache_slide_id = f"{study_id}:{slide_id}" if study_id else slide_id
    cached = await tile_cache.get_tile(cache_slide_id, z, x, y)
    if cached:
        return Response(content=cached, media_type="image/jpeg",
                        headers=TILE_CACHE_HEADERS)

    try:
        cache_key = tile_cache.tile_cache_key(cache_slide_id, z, x, y)

        async def _build_tile():
            image_bytes = await _run_slide_image_operation(
                slide_id,
                study_id,
                get_tile_bytes,
                z,
                x,
                y,
                operation_kind="tile",
            )
            await tile_cache.set_tile(cache_slide_id, z, x, y, image_bytes)
            return image_bytes

        data = await _singleflight.do(cache_key, "tile", _build_tile)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except OverviewTooLarge as exc:
        logger.warning(
            "Tile overview rejected for slide %s z=%d x=%d y=%d level=%d read=%dx%d pixels=%d limit=%d",
            slide_id,
            z,
            x,
            y,
            exc.best_level,
            exc.read_width,
            exc.read_height,
            exc.requested_pixels,
            exc.max_decode_pixels,
        )
        DECODE_SOURCE_PIXELS.observe(exc.requested_pixels)
        OVERSIZED_DECODE_REJECTIONS.labels(kind="tile").inc()
        raise HTTPException(status_code=422, detail={"error": "overview_requires_preprocessing"})
    except Exception:
        logger.exception("Tile extraction failed for %s z=%d x=%d y=%d",
                         slide_id, z, x, y)
        raise HTTPException(status_code=500, detail="Tile extraction failed")
    return Response(content=data, media_type="image/jpeg",
                    headers=TILE_CACHE_HEADERS)
