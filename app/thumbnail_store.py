from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

import boto3
import fsspec
from botocore.config import Config
from PIL import Image

from .config import settings
from .slide_store import s3_opts


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ThumbnailRecord:
    """A published thumbnail master, named by its sealed URI."""

    uri: str


class InvalidThumbnail(Exception):
    """The stored thumbnail is not an image Pillow can read."""


def _filesystem_for_uri(uri: str):
    if uri.startswith("s3://"):
        return fsspec.filesystem("s3", **s3_opts())
    return fsspec.filesystem("file")


def _filesystem_path(uri: str) -> str:
    if uri.startswith("s3://"):
        return uri[5:]
    if uri.startswith("file://"):
        return uri[7:]
    return uri


_runtime_s3_client = None
_runtime_s3_lock = threading.Lock()


def _s3_location(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise FileNotFoundError("Malformed thumbnail URI")
    return parsed.netloc, parsed.path.lstrip("/")


def _new_runtime_s3_client():
    config = Config(
        max_pool_connections=max(1, settings.thumbnail_s3_max_connections),
        connect_timeout=max(0.1, settings.thumbnail_s3_connect_timeout_sec),
        read_timeout=max(0.1, settings.thumbnail_s3_read_timeout_sec),
        retries={
            "mode": "standard",
            "max_attempts": max(1, settings.thumbnail_s3_max_attempts),
        },
        s3={"addressing_style": "path"},
    )
    return boto3.client(
        "s3",
        endpoint_url=settings.aws_endpoint_url or None,
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        config=config,
    )


def initialize_runtime_store() -> None:
    """Create and optionally prewarm one S3 client per application worker."""
    global _runtime_s3_client
    with _runtime_s3_lock:
        if _runtime_s3_client is None:
            _runtime_s3_client = _new_runtime_s3_client()
        client = _runtime_s3_client
    prewarm_uri = settings.thumbnail_prewarm_uri.strip()
    if prewarm_uri:
        bucket, key = _s3_location(prewarm_uri)
        client.head_object(Bucket=bucket, Key=key)
        response = client.get_object(Bucket=bucket, Key=key, Range="bytes=0-4095")
        body = response["Body"]
        try:
            body.read()
        finally:
            body.close()


def close_runtime_store() -> None:
    global _runtime_s3_client
    with _runtime_s3_lock:
        client = _runtime_s3_client
        _runtime_s3_client = None
    if client is not None:
        close = getattr(client, "close", None)
        if close is not None:
            close()


def _runtime_s3() -> Any:
    global _runtime_s3_client
    if _runtime_s3_client is None:
        initialize_runtime_store()
    return _runtime_s3_client


def read_thumbnail_bytes(record: ThumbnailRecord) -> bytes:
    if record.uri.startswith("s3://"):
        bucket, key = _s3_location(record.uri)
        try:
            response = _runtime_s3().get_object(Bucket=bucket, Key=key)
            body = response["Body"]
            try:
                return body.read()
            finally:
                body.close()
        except Exception as exc:
            # ECS accepts the s3fs/fsspec path used by the slide reader even
            # when the pooled boto3 client cannot complete a direct object
            # read from a worker.  Keep the exact published variant URI and
            # fall back to that known-good transport before reporting the
            # thumbnail as unavailable.
            logger.warning(
                "Direct thumbnail object read failed; retrying through fsspec; error_type=%s",
                type(exc).__name__,
            )
            fs = _filesystem_for_uri(record.uri)
            with fs.open(_filesystem_path(record.uri), "rb") as handle:
                return handle.read()
    fs = _filesystem_for_uri(record.uri)
    with fs.open(_filesystem_path(record.uri), "rb") as handle:
        return handle.read()


def _check_decode_size(image: Image.Image) -> None:
    width, height = image.size
    if width * height > settings.thumbnail_max_decode_pixels:
        raise InvalidThumbnail("ThumbnailTooLarge")


def render_thumbnail_payload(
    payload: bytes,
    requested_width: int,
    requested_height: int,
) -> tuple[bytes, dict[str, Any]]:
    """Fit the stored thumbnail into the requested box; never upscale.

    The header is checked against ``THUMBNAIL_MAX_DECODE_PIXELS`` before any
    pixel is decoded.  A JPEG that already fits is returned byte for byte;
    any other format is re-encoded to JPEG, since the response is
    ``image/jpeg``.  A JPEG that needs shrinking is decoded at reduced scale
    through ``Image.draft``.
    """
    try:
        with Image.open(BytesIO(payload)) as image:
            _check_decode_size(image)
            fits = requested_width >= image.width and requested_height >= image.height
            if fits and image.format == "JPEG":
                return payload, {"status": "ok", "reason": "master"}
            if not fits:
                image.draft("RGB", (requested_width, requested_height))
            working = image.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidThumbnail(type(exc).__name__) from None
    if fits:
        reason = "reencoded"
    else:
        working.thumbnail((requested_width, requested_height), Image.Resampling.LANCZOS)
        reason = "resized"
    out = BytesIO()
    working.save(out, format="JPEG", quality=settings.jpeg_quality)
    return out.getvalue(), {"status": "ok", "reason": reason}
