from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import fsspec
from PIL import Image
from PIL import UnidentifiedImageError

from .config import settings
from .slide_store import s3_opts


@dataclass(frozen=True)
class ThumbnailRecord:
    image_id: str
    uri: str
    width: int
    height: int
    content_type: str = "image/jpeg"


class ThumbnailArtifactTooLarge(ValueError):
    """Raised when a persisted thumbnail exceeds the configured byte budget."""


class UnsafeThumbnailArtifact(ValueError):
    """Raised when a persisted thumbnail is not a safe bounded JPEG."""


def _filesystem_for_uri(uri: str):
    if uri.startswith("s3://"):
        return fsspec.filesystem("s3", **s3_opts())
    return fsspec.filesystem("file")


def _is_manifest_artifact_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme.lower() == "s3":
        return bool(parsed.netloc and parsed.path.strip("/"))
    return parsed.scheme.lower() == "file" and settings.allow_file_sources


def _filesystem_path(uri: str) -> str:
    if uri.startswith("s3://"):
        return uri[5:]
    if uri.startswith("file://"):
        return uri[7:]
    return uri


def _load_manifest() -> dict[str, ThumbnailRecord]:
    manifest_uri = settings.thumbnail_manifest_uri.strip()
    if not manifest_uri or not _is_manifest_artifact_uri(manifest_uri):
        return {}
    fs = _filesystem_for_uri(manifest_uri)
    try:
        with fs.open(_filesystem_path(manifest_uri), "r") as handle:
            raw = handle.read(settings.thumbnail_manifest_max_bytes + 1)
    except FileNotFoundError:
        return {}
    if len(raw.encode("utf-8")) > settings.thumbnail_manifest_max_bytes:
        raise ThumbnailArtifactTooLarge("thumbnail manifest exceeds byte budget")
    payload = json.loads(raw)
    slides = payload.get("slides") or {}
    records: dict[str, ThumbnailRecord] = {}
    for image_id, raw in slides.items():
        if not isinstance(raw, dict):
            continue
        uri = str(raw.get("uri") or "").strip()
        if not uri or not _is_manifest_artifact_uri(uri):
            continue
        records[str(image_id)] = ThumbnailRecord(
            image_id=str(image_id),
            uri=uri,
            width=max(1, int(raw.get("width") or settings.thumbnail_master_size)),
            height=max(1, int(raw.get("height") or settings.thumbnail_master_size)),
            content_type=str(raw.get("content_type") or "image/jpeg"),
        )
    return records


class ThumbnailManifestCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._records: dict[str, ThumbnailRecord] = {}
        self._generated_records: OrderedDict[str, ThumbnailRecord] = OrderedDict()

    def get(self, image_id: str) -> ThumbnailRecord | None:
        self._refresh_if_needed()
        manifest_record = self._records.get(image_id)
        if manifest_record is not None:
            return manifest_record
        with self._lock:
            generated = self._generated_records.get(image_id)
            if generated is not None:
                self._generated_records.move_to_end(image_id)
            return generated

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = 0.0
            self._records = {}
            self._generated_records.clear()

    def register_generated(self, record: ThumbnailRecord) -> None:
        with self._lock:
            capacity = max(0, settings.thumbnail_generated_record_cache_capacity)
            if capacity == 0:
                return
            self._generated_records.pop(record.image_id, None)
            self._generated_records[record.image_id] = record
            while len(self._generated_records) > capacity:
                self._generated_records.popitem(last=False)

    def _refresh_if_needed(self) -> None:
        refresh_sec = max(0, settings.thumbnail_manifest_refresh_sec)
        now = time.monotonic()
        with self._lock:
            if self._records and refresh_sec and now - self._loaded_at < refresh_sec:
                return
            self._records = _load_manifest()
            self._loaded_at = now


manifest_cache = ThumbnailManifestCache()


def get_thumbnail_record(image_id: str) -> ThumbnailRecord | None:
    return manifest_cache.get(image_id)


def _thumbnail_root_uri() -> str:
    manifest_uri = settings.thumbnail_manifest_uri.strip()
    if not manifest_uri or not _is_manifest_artifact_uri(manifest_uri):
        return ""
    if manifest_uri.endswith("/manifest.json"):
        return manifest_uri[: -len("/manifest.json")] + "/masters"
    if manifest_uri.endswith("manifest.json"):
        return str(PurePosixPath(manifest_uri).parent) + "/masters"
    return manifest_uri.rstrip("/") + "/masters"


def _generated_thumbnail_uri(image_id: str) -> str:
    root_uri = _thumbnail_root_uri()
    if not root_uri:
        return ""
    return root_uri.rstrip("/") + f"/{image_id}.jpg"


def _read_thumbnail_dimensions(payload: bytes) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(payload)) as image:
            width, height = image.size
            if width < 1 or height < 1 or width * height > settings.thumbnail_max_decode_pixels:
                raise UnsafeThumbnailArtifact("thumbnail dimensions exceed pixel budget")
            if image.format != "JPEG":
                raise UnsafeThumbnailArtifact("thumbnail is not JPEG")
            image.verify()
            return width, height
    except UnsafeThumbnailArtifact:
        raise
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise UnsafeThumbnailArtifact("thumbnail is not a valid JPEG") from exc


def _record_from_payload(image_id: str, uri: str, payload: bytes) -> ThumbnailRecord:
    width, height = _read_thumbnail_dimensions(payload)
    return ThumbnailRecord(
        image_id=image_id,
        uri=uri,
        width=max(1, width),
        height=max(1, height),
        content_type="image/jpeg",
    )


def get_persisted_generated_thumbnail_record(image_id: str) -> ThumbnailRecord | None:
    uri = _generated_thumbnail_uri(image_id)
    if not uri:
        return None
    fs = _filesystem_for_uri(uri)
    path = _filesystem_path(uri)
    if not fs.exists(path):
        return None
    with fs.open(path, "rb") as handle:
        payload = handle.read(settings.thumbnail_artifact_max_bytes + 1)
    if len(payload) > settings.thumbnail_artifact_max_bytes:
        raise ThumbnailArtifactTooLarge("thumbnail artifact exceeds byte budget")
    record = _record_from_payload(image_id, uri, payload)
    manifest_cache.register_generated(record)
    return record


def store_generated_thumbnail(image_id: str, payload: bytes) -> ThumbnailRecord | None:
    uri = _generated_thumbnail_uri(image_id)
    if not uri:
        return None
    if len(payload) > settings.thumbnail_artifact_max_bytes:
        raise ThumbnailArtifactTooLarge("thumbnail artifact exceeds byte budget")
    _read_thumbnail_dimensions(payload)
    fs = _filesystem_for_uri(uri)
    path = _filesystem_path(uri)
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    protocols = fs.protocol if isinstance(fs.protocol, (tuple, list)) else (fs.protocol,)
    if parent and "file" in protocols:
        fs.makedirs(parent, exist_ok=True)
    with fs.open(path, "wb") as handle:
        handle.write(payload)
    record = _record_from_payload(image_id, uri, payload)
    manifest_cache.register_generated(record)
    return record


def read_thumbnail_bytes(record: ThumbnailRecord) -> bytes:
    fs = _filesystem_for_uri(record.uri)
    with fs.open(_filesystem_path(record.uri), "rb") as handle:
        payload = handle.read(settings.thumbnail_artifact_max_bytes + 1)
    if len(payload) > settings.thumbnail_artifact_max_bytes:
        raise ThumbnailArtifactTooLarge("thumbnail artifact exceeds byte budget")
    return payload


def render_thumbnail_response(
    record: ThumbnailRecord,
    requested_width: int,
    requested_height: int,
) -> tuple[bytes, dict[str, Any]]:
    payload = read_thumbnail_bytes(record)
    actual_width, actual_height = _read_thumbnail_dimensions(payload)
    if requested_width >= actual_width and requested_height >= actual_height:
        return payload, {"status": "ok", "reason": "master"}

    try:
        with Image.open(BytesIO(payload)) as image:
            working = image.convert("RGB")
            working.thumbnail((requested_width, requested_height), Image.Resampling.LANCZOS)
            out = BytesIO()
            working.save(out, format="JPEG", quality=settings.jpeg_quality)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise UnsafeThumbnailArtifact("thumbnail cannot be resized") from exc
    resized_payload = out.getvalue()
    if len(resized_payload) > settings.thumbnail_artifact_max_bytes:
        raise ThumbnailArtifactTooLarge("resized thumbnail exceeds byte budget")
    return resized_payload, {"status": "ok", "reason": "resized"}
